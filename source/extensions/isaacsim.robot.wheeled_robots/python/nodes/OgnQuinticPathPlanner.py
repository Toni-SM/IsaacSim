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

import numpy as np
import omni
import omni.graph.core as og
from isaacsim.core.nodes import BaseResetNode
from isaacsim.core.utils.rotations import quat_to_euler_angles
from isaacsim.robot.wheeled_robots.controllers.quintic_path_planner import quintic_polynomials_planner
from isaacsim.robot.wheeled_robots.controllers.stanley_control import normalize_angle
from isaacsim.robot.wheeled_robots.ogn.OgnQuinticPathPlannerDatabase import OgnQuinticPathPlannerDatabase


class OgnQuinticPathPlannerInternalState(BaseResetNode):
    # modeled after OgnDifferentialController state layout
    def __init__(self):
        self.stage = omni.usd.get_context().get_stage()  # save stage for getting prim at path
        self.target = []  # stored target pos to avoid need for recalculation each cycle - [x, y, z_rot]
        self.node = None
        self.rx = []  # stored path array outputs to avoid retargeting each cycle
        self.ry = []
        self.ryaw = []
        self.rv = []
        super().__init__(initialize=False)

    def custom_reset(self):  # reset all saved values to prevent carrying over into different run
        self.target = []
        self.rx = []
        self.ry = []
        self.ryaw = []
        self.rv = []


class OgnQuinticPathPlanner:
    @staticmethod
    def init_instance(node, graph_instance_id):
        state = OgnQuinticPathPlannerDatabase.get_internal_state(node, graph_instance_id)
        state.node = node

    @staticmethod
    def internal_state():
        return OgnQuinticPathPlannerInternalState()

    @staticmethod
    def compute(db) -> bool:
        state = db.per_instance_state

        # calculate and save relevant target data from inputs, will be None if target has not changed
        goal = get_target_pos(db.inputs, state)

        # get robot pos/rot
        pos = db.inputs.currentPosition
        x = pos[0]
        y = pos[1]
        _, _, rot = quatd4_to_euler(db.inputs.currentOrientation)

        # if target has changed
        if goal is not None:
            state.target = goal  # save new target

            # run quintic polynomial planner and save path arrays
            # rv = velocity, rx = x position, ry = y position, ryaw = yaw value (absolute rotation, not delta)
            _, state.rx, state.ry, state.ryaw, state.rv, _, _ = quintic_polynomials_planner(
                x,
                y,
                rot,
                db.inputs.initialVelocity,
                db.inputs.initialAccel,
                state.target[0],
                state.target[1],
                state.target[2],
                db.inputs.goalVelocity,
                db.inputs.goalAccel,
                db.inputs.maxAccel,
                db.inputs.maxJerk,
                db.inputs.step,
            )

            # convert to np array for output
            state.target = np.array(state.target)

        # concatenate path arrays together to use only one input/output instead of 4 -> reduces runtime
        db.outputs.pathArrays = np.array(state.rv + state.rx + state.ry + state.ryaw)
        db.outputs.target = state.target  # output new or saved target
        db.outputs.targetChanged = goal is not None  # output True if target has changed
        db.outputs.execOut = og.ExecutionAttributeState.ENABLED  # begin next node (check goal)

        return True


def get_target_pos(inputs, state):
    g = []
    if len(inputs.targetPrim) == 0:  # if targetPrim not provided
        pos = inputs.targetPosition  # use double[3] position input
        _, _, rot = quatd4_to_euler(inputs.targetOrientation)  # and quaternion rotation input
        g = [pos[0], pos[1], rot]  # combine into list of useful data
    else:  # if targetPrim is provided
        prim = state.stage.GetPrimAtPath(inputs.targetPrim[0].GetString())  # get targetPrim
        m = omni.usd.get_world_transform_matrix(prim)  # get position/rotation matrix of targetPrim
        m.Orthonormalize()  # normalize vectors and make orthogonal
        pos = list(m.ExtractTranslation())  # get position double[3]
        rot = normalize_angle(np.radians(m.ExtractRotation().angle))  # get rotation double
        g = [pos[0], pos[1], rot]  # combine into list of useful data

    if (
        len(state.target) > 0
        and abs(g[0] - state.target[0]) < 0.1
        and abs(g[1] - state.target[1]) < 0.1
        and abs(g[2] - state.target[2]) < 0.05
    ):  # if target diff from saved target is small enough to be negligible error
        return None
    else:
        return g  # signals to compute() method that target has changed, provides new target


def quatd4_to_euler(orientation):
    # implementation for quat_to_euler_angles that normalizes outputs
    x, y, z, w = tuple(orientation)
    roll, pitch, yaw = quat_to_euler_angles(np.array([w, x, y, z]))

    return normalize_angle(roll), normalize_angle(pitch), normalize_angle(yaw)
