# SPDX-FileCopyrightText: Copyright (c) 2022-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import isaacsim.cortex.framework.math_util as math_util
import numpy as np
from isaacsim.cortex.framework.df import (
    DfAction,
    DfDecider,
    DfDecision,
    DfNetwork,
    DfState,
    DfStateMachineDecider,
    DfStateSequence,
    DfTimedDeciderState,
    DfWriteContextState,
)
from isaacsim.cortex.framework.dfb import DfLift, DfRobotApiContext
from isaacsim.cortex.framework.motion_commander import ApproachParams, PosePq


def sample_target_p():
    min_x = 0.3
    max_x = 0.7
    min_y = -0.4
    max_y = 0.4

    pt = np.zeros(3)
    pt[0] = (max_x - min_x) * np.random.random_sample() + min_x
    pt[1] = (max_y - min_y) * np.random.random_sample() + min_y
    pt[2] = 0.01

    return pt


def make_target_rotation(target_p):
    return math_util.matrix_to_quat(
        math_util.make_rotation_matrix(az_dominant=np.array([0.0, 0.0, -1.0]), ax_suggestion=-target_p)
    )


class PeckContext(DfRobotApiContext):
    def __init__(self, robot):
        super().__init__(robot)
        self.robot = robot
        self.reset()
        self.add_monitors([PeckContext.monitor_active_target_p])

    def reset(self):
        self.is_done = True
        self.active_target_p = None

    def monitor_active_target_p(self):
        if self.active_target_p is not None and self.is_near_obs(self.active_target_p):
            self.is_done = True

    def set_is_done(self):
        self.is_done = True

    def is_near_obs(self, p):
        for _, obs in self.robot.registered_obstacles.items():
            obs_p, _ = obs.get_world_pose()
            if np.linalg.norm(obs_p - p) < 0.2:
                return True
        return False

    def sample_target_p_away_from_obs(self):
        target_p = sample_target_p()
        while self.is_near_obs(target_p):
            target_p = sample_target_p()
        return target_p

    def choose_next_target(self):
        self.active_target_p = self.sample_target_p_away_from_obs()


class PeckState(DfState):
    def enter(self):
        target_p = self.context.active_target_p
        target_q = make_target_rotation(target_p)
        self.target = PosePq(target_p, target_q)
        approach_params = ApproachParams(direction=np.array([0.0, 0.0, -0.1]), std_dev=0.04)
        self.context.robot.arm.send_end_effector(self.target, approach_params=approach_params)

    def step(self):
        # Send the command each cycle so exponential smoothing will converge.
        target_dist = np.linalg.norm(self.context.robot.arm.get_fk_p() - self.target.p)
        if target_dist < 0.01:
            return None  # Exit
        return self  # Keep going


class ChooseTarget(DfAction):
    def step(self):
        self.context.is_done = False
        self.context.choose_next_target()


class CloseGripper(DfAction):
    def enter(self):
        self.context.robot.gripper.close()


class Dispatch(DfDecider):
    """The top-level decider.

    If the current peck task is done, then it will choose a target.  Otherwise, it executes the peck
    behavior. The peck behavior is a sequential state machine which 1. closes the gripper, 2. pecks,
    3. lifts the end-effector slightly, 4. writes to the context that it's done.

    This behavior by itself is equivalent to the state machine variant in peck_state_machine.py.
    However, the context is also continually monitoring the situation and if it sees that its
    current target is blocked, it'll set the context.is_done flag to True triggering this Dispatch
    decider to choose a new target.
    """

    def __init__(self):
        super().__init__()

        self.add_child("choose_target", ChooseTarget())
        self.add_child(
            "peck",
            DfStateMachineDecider(
                DfStateSequence(
                    [
                        CloseGripper(),
                        PeckState(),
                        DfTimedDeciderState(DfLift(height=0.05), activity_duration=0.25),
                        DfWriteContextState(lambda context: context.set_is_done()),
                    ]
                )
            ),
        )

    def decide(self):
        if self.context.is_done:
            return DfDecision("choose_target")
        else:
            return DfDecision("peck")


def make_decider_network(robot):
    return DfNetwork(Dispatch(), context=PeckContext(robot))
