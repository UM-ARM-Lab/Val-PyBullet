from dataclasses import dataclass
import cv2
from visual_servoing.pbvs import CheaterPBVS
from visual_servoing.marker_pbvs import MarkerPBVS
from visual_servoing.val import Val
from visual_servoing.utils import *
from visual_servoing.camera import PyBulletCamera
from visual_servoing.mppi_vs import VisualServoMPPI
from visual_servoing.utils import quaternion_to_axis_angle
import numpy as np
import pybullet as p
import torch

val = Val([0.0, 0.0, -0.5])
camera = PyBulletCamera(camera_eye=np.array([0.7, -0.8, 0.5]), camera_look=np.array([0.7, 0.0, 0.2]))
p.setGravity(0, 0, -10)

def get_interaction_mat(u, v, z):
    return np.array([
        [-1/z, 0, u/z, u*v, -1* (1+u**2), v],
        [0, -1/z, v/z, (1+v**2), -u*v, -u]
    ])

# Specify the 3D geometry of the end effector marker board 
tag_len = 0.0305
gap_len = 0.0051
angle = np.pi / 4
# center tag
tag0_tl = np.array([-tag_len / 2, tag_len / 2, 0.0], dtype=np.float32)
tag0_tr = np.array([tag_len / 2, tag_len / 2, 0.0], dtype=np.float32)
tag0_br = np.array([tag_len / 2, -tag_len / 2, 0.0], dtype=np.float32)
tag0_bl = np.array([-tag_len / 2, -tag_len / 2, 0.0], dtype=np.float32)
z1 = -np.cos(angle) * gap_len
z2 = -np.cos(angle) * (gap_len + tag_len)
y1 = tag_len / 2 + gap_len + gap_len * np.sin(angle)
y2 = tag_len / 2 + gap_len + (gap_len + tag_len) * np.sin(angle)
# lower tag
tag1_tl = np.array([-tag_len / 2, -y1, z1], dtype=np.float32)
tag1_tr = np.array([tag_len / 2, -y1, z1], dtype=np.float32)
tag1_br = np.array([tag_len / 2, -y2, z2], dtype=np.float32)
tag1_bl = np.array([-tag_len / 2, -y2, z2], dtype=np.float32)
# upper tag
tag2_tl = np.array([-tag_len / 2, y2, z2], dtype=np.float32)
tag2_tr = np.array([tag_len / 2, y2, z2], dtype=np.float32)
tag2_br = np.array([tag_len / 2, y1, z1], dtype=np.float32)
tag2_bl = np.array([-tag_len / 2, y1, z1], dtype=np.float32)

tag0 = np.array([tag0_tl, tag0_tr, tag0_br, tag0_bl])
tag1 = np.array([tag1_tl, tag1_tr, tag1_br, tag1_bl])
tag2 = np.array([tag2_tl, tag2_tr, tag2_br, tag2_bl])
tag_geometry = [tag0, tag1, tag2]
ids = np.array([1, 2, 3])
ids2 = np.array([4,5,6])

#pbvs = MarkerPBVS(camera, 1, 1, 1.5, np.eye(4), ids, tag_geometry, ids2, tag_geometry)
def get_eef_gt(robot, quat=False):
    '''
    Gets the ground truth pose of the end effector from the simulator
    '''
    tool_idx = robot.left_tag[0]
    result = p.getLinkState(robot.urdf,
                            tool_idx,
                            computeLinkVelocity=1,
                            computeForwardKinematics=1)

    link_trn, link_rot, com_trn, com_rot, frame_pos, frame_rot, link_vt, link_vr = result
    Twe = np.eye(4)
    Twe[0:3, 0:3] = np.array(p.getMatrixFromQuaternion(frame_rot)).reshape(3, 3)
    Twe[0:3, 3] = frame_pos
    if(quat):
        return link_trn, link_rot
    return Twe
#pbvs = CheaterPBVS(camera, 1, 1, 1.5, lambda : get_eef_gt(val))

def add_global_twist(twist, pose, dt):
    r3 = dt * twist[:3]
    so3, _ = cv2.Rodrigues(dt * twist[3:])
    tf = np.eye(4)
    tf[0:3, 0:3] = so3 @ pose[0:3, 0:3]
    tf[0:3, 3] = r3 + pose[0:3, 3]
    return tf

def reproject(Tcm, point_marker, camera):
    # Get another pt
    pt2_cam = Tcm @ point_marker
    pt2_im = camera.get_intrinsics() @ pt2_cam[0:3]
    pt2_im /= pt2_im[2]
    px2 = pt2_im[0:2].astype(int)
    return px2, pt2_cam


@dataclass
class ArmDynamics:
    target_pos : np.ndarray # r3
    target_rot : np.ndarray # quat 
    J : np.ndarray
    dt : float

    def running_cost(self, x : torch.Tensor, u : torch.Tensor):
        pos_error = torch.norm((x[:, 0:3] - self.target_pos), 2, dim=1)
        ctrl_error = torch.norm(u, 2, dim=1)
        target_rot = torch.tensor(self.target_rot, device='cuda', dtype=torch.float32)
        rot_delta = quaternion_multiply(quaternion_invert(x[:, 3:7]), target_rot.reshape((1, -1)))
        rot_error = torch.norm(rot_delta ,2, dim=1)
        return pos_error # + rot_error #+ ctrl_error +

    def batchable_dynamics_arm(self, x : torch.Tensor, u : torch.Tensor):
        """
        Arm dynamics function
        x_(t+1) = x_t + dt J q'
        x : K x nx
        u : K x nu

        x is a vector [p r]' which has an R3 position and quaternion
        u is a vector of joint vleocities q'
        """
        # Compute velocity in state vector, p' and omega
        x_dot = (self.J @ u.T).T
        x_next = torch.zeros(x.shape, device="cuda", dtype=torch.float32)

        # Update position 
        x_next[:, 0:3] = x[:, 0:3] + x_dot[:, 0:3] * self.dt

        # Get the action delta as a quaternion
        # Note that action TFs are in global frame
        action_quat = axis_angle_to_quaternion(self.dt * x_dot[:, 3:6]).reshape(1, -1)
        
        # Update rot
        #x_next[:, 3:7] = quaternion_multiply(action_quat, x[:, 3:7])

        return x_next

def get_homogenous(se3):
    Twe = np.eye(4)
    Twe[0:3, 0:3] = np.array(p.getMatrixFromQuaternion(se3[3:7])).reshape(3, 3)
    Twe[0:3, 3] = se3[0:3]
    return Twe

camera_pose_vis = PoseVisualizer()
eef_pose_vis = PoseVisualizer()
eef_pose_pred_vis = PoseVisualizer()
eef_pose_joint_vis = PoseVisualizer()
target_pose_vis = PoseVisualizer()

# Target
Two = np.eye(4) 
Two[0:3, 3] = np.array([-0.2, 0.4, -0.1])
Two[0:3, 0:3] = np.array(p.getMatrixFromQuaternion(p.getQuaternionFromEuler((np.pi/2, np.pi/2, np.pi/2)))).reshape(3, 3) # hard

J = val.get_arm_jacobian("left", True)

pbvs = CheaterPBVS(camera, 1, 1, 0.1, lambda : get_eef_gt(val))

mppi = VisualServoMPPI(dt=0.1, eef_target_baselink= np.linalg.inv(val.get_link_pose(0)) @ Two)


# DELETE
ctrl_steps = 0
p.setTimeStep(1/240)

while(True):
    if(ctrl_steps == 0):
        rgb, depth = camera.get_image()

        jac = val.get_arm_jacobian("left", True)
        jac_pinv = val.get_jacobian_pinv("left", True)
        print(jac)

        # Send command to val

        ctrl, Twe = pbvs.do_pbvs(rgb, depth, Two, np.eye(4), jac, jac_pinv, 24) 

        ctrl_limit = pbvs.limit_twist(jac, jac_pinv, ctrl)
        q_dot = jac_pinv @ ctrl_limit
        #q_dot[8] = 0
        val.velocity_control("left", q_dot, True)
        start_joint_config = val.get_joint_states_left() 

    cur_joint_config = val.get_joint_states_left() 

    val.velocity_control("left", q_dot, True)
    q_dot = np.zeros(9)
    q_dot[0] = 0.1
    q_dot[1] = -0.1
    #q_dot[4] = -0.1
    q_dot[7] = -0.1
    q_dot[8] = 0.1

    val.velocity_control("left", q_dot, True)
    #val.pos_vel_control("left", q_dot, start_joint_config + q_dot * 0.1*(ctrl_steps), True)

    #print(q_dot)
    #print(val.get_joint_vel_left())
    print(np.max(val.get_joint_vel_left() - q_dot))
    print(np.argmax(val.get_joint_vel_left() - q_dot))

    step = 5
    if( ctrl_steps % step == 0):
        Twe = get_eef_gt(val)
        cur_joint_config = val.get_joint_states_left() 
        #x = val.get_link_pose(0) @ (mppi.chain.forward_kinematics(cur_joint_config).get_matrix()[0]).cpu().numpy()
        Twe_pred, q_pred = mppi.arm_dynamics_tester(Twe, cur_joint_config, q_dot, val.get_link_pose(0), step)
        Twe_pred_joint = val.get_link_pose(0) @ (mppi.chain.forward_kinematics(q_pred).get_matrix()[0]).cpu().numpy()
        eef_pose_pred_vis.update(Twe_pred)
        #eef_pose_joint_vis.update(Twe_pred_joint)

    Twe_prev = Twe.copy()

    a = 1
    # Step sim
    for _ in range(24):
        #link_pos, link_rot = val.get_eef_pos("camera")
        #camera_rot = np.array(p.getMatrixFromQuaternion(link_rot)).reshape(3,3)
        #camera.upate_from_pose(link_pos, camera_rot)
        p.stepSimulation()

    # Visualize current eef pose
    Twe = get_eef_gt(val)
    eef_pose_vis.update(Twe)
    Twc = np.linalg.inv(camera.get_extrinsics())
    ctrl_steps += 1

    #camera_pose_vis.update(Twc)
    target_pose_vis.update(Two)
    #ax, _ = cv2.Rodrigues((Twe @ np.linalg.inv(Twe_prev))[:3, :3])  
    #print( f"ground truth: \n ax: {ax / np.linalg.norm(ax)}, angle: {np.linalg.norm(ax)}" ) 
    #print( f"MPPI: \n ax: {mppi.world_axis / torch.linalg.norm(mppi.world_axis)}, angle: {mppi.dt*torch.linalg.norm(mppi.world_axis)}" ) 
    #ax_gt = ax.squeeze()
    #ax_mes = mppi.world_axis.squeeze().numpy() * mppi.dt
    #ax_diff = np.arccos(np.dot(ax_gt, ax_mes) / (np.linalg.norm(ax_gt) * np.linalg.norm(ax_mes))) * 180/np.pi
    #print(f"axis variation: {ax_diff} degrees -- magnitude variation { 100* (np.linalg.norm(ax_gt) - np.linalg.norm(ax_mes))/np.linalg.norm(ax_mes)}%")
    #print("next...")