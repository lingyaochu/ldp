import itertools
import random
import tempfile
from copy import deepcopy
from typing import Any, cast

import pytest
from aviary.env import Environment, Frame
from aviary.message import Message
from aviary.tools import Tool, ToolRequestMessage
from pydantic import BaseModel

from ldp.agent import Agent, SimpleAgent, SimpleAgentState
from ldp.alg.beam_search import BeamSearchRollout
from ldp.alg.callbacks import Callback
from ldp.alg.rollout import RolloutManager
from ldp.alg.tree_search import TreeSearchRollout
from ldp.data_structures import Trajectory, Transition, TransitionTree
from ldp.graph.common_ops import FxnOp
from ldp.graph.op_utils import compute_graph, set_training_mode
from ldp.graph.ops import OpResult


class DummyEnv(Environment[None]):
    def __init__(self):
        self.tools = [Tool.from_function(self.talk)]

    async def reset(self) -> tuple[list[Message], list[Tool]]:
        return [Message(content="Hello!")], self.tools

    async def step(
        self, action: ToolRequestMessage
    ) -> tuple[list[Message], float, bool, bool]:
        if action.tool_calls:
            responses = cast(list[Message], await self.exec_tool_calls(action))
        else:
            responses = [Message(content="Use the 'talk' tool to speak.")]

        return responses, 0.0, False, False

    async def talk(self, message: str) -> str:
        """Say something to me.

        Args:
            message (str): what you want to say

        Returns:
            str: my response
        """
        return message

    def export_frame(self) -> Frame:
        return Frame()


async def count_exclamations(traj: Trajectory) -> float:
    last_step = traj.steps[-1]
    agent_state = cast(SimpleAgentState, last_step.next_agent_state)
    return float(
        sum(m.content.count("!") for m in agent_state.messages if m.content is not None)
    )


@pytest.mark.parametrize("training", [True, False])
@pytest.mark.asyncio
async def test_rollout(training: bool) -> None:
    agent = SimpleAgent()
    callback = DummyCallback()
    set_training_mode(training)
    rollout_manager = RolloutManager(
        agent,
        catch_agent_failures=False,
        catch_env_failures=False,
        callbacks=[callback],
    )
    trajs = await rollout_manager.sample_trajectories(
        environments=[DummyEnv(), DummyEnv()], max_steps=1
    )
    assert len(trajs) == 2

    # Let's check we can serialize and deserialize the trajectories
    for traj in trajs:
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            traj.to_jsonl(filename=f.name)
            rehydrated_traj = Trajectory.from_jsonl(f.name)
            assert traj.traj_id == rehydrated_traj.traj_id

    assert all(v == 2 for v in callback.fn_invocations.values())


async def adeepcopy(x):
    return deepcopy(x)


@pytest.mark.asyncio
async def test_beam_search() -> None:
    agent = SimpleAgent()
    callback = DummyCallback()
    beam_search = BeamSearchRollout(
        agent,
        beam_width=1,  # keep these numbers small to speed up test
        samples_per_beam=1,
        env_clone_fn=adeepcopy,
        agent_clone_fn=deepcopy,
        scoring_fn=count_exclamations,
        catch_agent_failures=False,
        catch_env_failures=False,
        callbacks=[callback],
    )

    trajs = await beam_search.sample_trajectories(
        environments=[DummyEnv(), DummyEnv()], max_steps=1
    )
    assert len(trajs) == 2

    assert all(v == 2 for v in callback.fn_invocations.values())


class DummyCallback(Callback):
    def __init__(self):
        self.fn_invocations = {
            "before_transition": 0,
            "after_agent_get_asv": 0,
            "after_env_step": 0,
            "after_transition": 0,
        }

    async def before_transition(
        self,
        traj_id: str,
        agent: Agent,
        env: Environment,
        agent_state: Any,
        obs: list[Message],
    ) -> None:
        self.fn_invocations["before_transition"] += 1

    async def after_agent_get_asv(
        self,
        traj_id: str,
        action: OpResult[ToolRequestMessage],
        next_agent_state: Any,
        value: float,
    ):
        self.fn_invocations["after_agent_get_asv"] += 1

    async def after_env_step(
        self,
        traj_id: str,
        obs: list[Message],
        reward: float,
        done: bool,
        trunc: bool,
    ):
        self.fn_invocations["after_env_step"] += 1

    async def after_transition(
        self,
        traj_id: str,
        agent: Agent,
        env: Environment,
        transition: Transition,
    ) -> None:
        self.fn_invocations["after_transition"] += 1


class CountingAgentState(BaseModel):
    count: float = 0.0


class CountingAgent(Agent[CountingAgentState]):
    def __init__(self):
        self.op = FxnOp[ToolRequestMessage](lambda: ToolRequestMessage(tool_calls=[]))

    async def init_state(self, tools: list[Tool]) -> CountingAgentState:
        return CountingAgentState()

    @compute_graph()
    async def get_asv(
        self, agent_state: CountingAgentState, obs: list[Message]
    ) -> tuple[OpResult[ToolRequestMessage], CountingAgentState, float]:
        new_state = CountingAgentState(count=float(cast(str, obs[0].content)) + 1)
        action = await self.op()
        return action, new_state, 0.0


class CountingEnv(Environment[float]):
    def __init__(self, state: float = 0.0):
        self.state = state

    async def reset(self) -> tuple[list[Message], list[Tool]]:
        return [Message(content=str(self.state))], []

    async def step(
        self, action: ToolRequestMessage
    ) -> tuple[list[Message], float, bool, bool]:
        self.state += 1
        return [Message(content=str(self.state))], 0.0, self.state >= 3, False

    def export_frame(self) -> Frame:
        return Frame()


@pytest.mark.asyncio
async def test_deterministic_rollout():
    agent = CountingAgent()
    env = CountingEnv()

    rollout_manager = RolloutManager(agent)
    traj, *_ = await rollout_manager.sample_trajectories(environments=[env])

    assert len(traj.steps) == 3
    for i_step, step in enumerate(traj.steps):
        f_step = float(i_step)
        # check that we didn't clobber any agent or env states
        assert step.agent_state.count == f_step
        assert step.next_agent_state.count == f_step + 1
        assert step.observation[0].content == str(f_step)
        assert step.next_observation[0].content == str(f_step + 1)


class NoisyCountingEnv(CountingEnv):
    async def step(
        self, action: ToolRequestMessage
    ) -> tuple[list[Message], float, bool, bool]:
        self.state += 1 + random.uniform(-0.01, 0.01)
        return [Message(content=str(self.state))], 1.0, self.state >= 3, False


class TestTreeSearch:
    @pytest.mark.asyncio
    async def test_tree_search(self):
        agent = CountingAgent()
        # Use a slightly stochastic env so we can distinguish branches
        env = NoisyCountingEnv()

        callback = DummyCallback()
        rollout_manager = TreeSearchRollout(
            agent,
            branching_factor=2,
            env_clone_fn=deepcopy,
            concurrency_limit=1,
            callbacks=[callback],
        )
        tree = await rollout_manager.sample_tree(env, max_depth=3)
        trajs = tree.get_trajectories()
        assert len(trajs) == 8

        traj_ids_wo_root = {
            cast(str, traj.traj_id).replace(tree.root_id, "").lstrip(":")
            for traj in trajs
        }
        # IDs should be 0:0:0, 0:0:1, ... 1:1:1 (order doesn't matter)
        assert traj_ids_wo_root == {
            ":".join(x) for x in itertools.product("01", repeat=3)
        }

        observations = {}  # type: ignore[var-annotated]
        for traj in trajs:
            branch_path = tuple(cast(str, traj.traj_id).split(":")[1:])

            prev_step: Transition | None = None
            for i_step, step in enumerate(traj.steps):
                if prev_step is not None:
                    # Check that the child node started at the state emitted at the parent node
                    assert prev_step.next_agent_state == step.agent_state

                # Steps that started at the same node in the tree should have the same observation
                node_id = branch_path[: i_step + 1]
                if node_id in observations:
                    assert observations[node_id] == step.observation[0].content
                else:
                    observations[node_id] = step.observation[0].content

                prev_step = step

        # We expect sum_{i=1}^3 2^i = 2^4 - 2 = 14 transitions:
        # - branching factor = 2, depth = 3
        # - root node isn't sampled, so no i=0 term in sum
        assert all(v == 14 for v in callback.fn_invocations.values())

    @pytest.mark.asyncio
    async def test_early_stopping(self):
        agent = CountingAgent()
        # Use a slightly stochastic env so we can distinguish branches
        env = NoisyCountingEnv()

        callback = DummyCallback()
        rollout_manager = TreeSearchRollout(
            agent,
            branching_factor=2,
            env_clone_fn=deepcopy,
            concurrency_limit=1,
            callbacks=[callback],
            target_reward=0.5,
        )
        trajs = (await rollout_manager.sample_tree(env, max_depth=3)).get_trajectories()
        assert len(trajs) < 8  # should have exited early
        for traj in trajs:
            # should have hit target reward immediately
            assert len(traj.steps) == 1


def test_tree_mc_value():
    root_id = "dummy"
    tree = TransitionTree(root_id=root_id)

    kw = {
        "agent_state": None,
        "next_agent_state": None,
        "observation": Transition.NO_OBSERVATION,
        "next_observation": Transition.NO_OBSERVATION,
        "action": None,
    }

    # Construct a tree with some rewards scattered about
    tree.add_transition(f"{root_id}:0", Transition(timestep=0, reward=0.0, **kw))

    tree.add_transition(f"{root_id}:0:0", Transition(timestep=1, reward=1.0, **kw))
    for i in range(3):
        tree.add_transition(
            f"{root_id}:0:0:{i}",
            Transition(timestep=2, reward=float(i), done=True, **kw),
        )

    tree.add_transition(
        f"{root_id}:0:1", Transition(timestep=1, reward=-1.0, done=True, **kw)
    )

    tree.assign_mc_value_estimates(discount_factor=0.9)

    # Now make sure the value estimates are as expected
    # First, check the terminal nodes: Q==reward
    for i in range(3):
        assert tree.get_transition(f"{root_id}:0:0:{i}").value == float(i)
    assert tree.get_transition(f"{root_id}:0:1").value == -1.0

    # Then go up the tree
    assert tree.get_transition(f"{root_id}:0:0").value == pytest.approx(
        1 + 0.9 * ((0 + 1 + 2) / 3), rel=0.001
    )
    assert tree.get_transition(f"{root_id}:0").value == pytest.approx(
        0.0 + 0.9 * ((1.9 - 1) / 2), rel=0.001
    )
