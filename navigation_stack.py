import numpy as np
import time
import os
import yaml
from collections import deque
# import scipy

import rospy

from td3.td3 import *
from td3.net import *

from sensor_msgs.msg import LaserScan
from nav_msgs.msg import Path, Odometry
from geometry_msgs.msg import Twist


class StepRecorder:
    def __init__(self):
        self.X = 0
        self.Y = 0
        self.Z = 0
        self.PSI = 0

        self.laser_scan = None
        self.global_path = None

    def laser_scan_callback(self, msg: LaserScan):
        self.laser_scan = msg.ranges

    def robot_stat_callback(self, msg: Odometry):
        q1 = msg.pose.pose.orientation.x
        q2 = msg.pose.pose.orientation.y
        q3 = msg.pose.pose.orientation.z
        q0 = msg.pose.pose.orientation.w
        self.X = msg.pose.pose.position.x
        self.Y = msg.pose.pose.position.y
        self.Z = msg.pose.pose.position.z
        self.PSI = np.arctan2(2 * (q0*q3 + q1*q2), (1 - 2*(q2**2+q3**2)))

    def transform_lg(self, wp, X, Y, PSI):
        R_r2i = np.matrix([[np.cos(PSI), -np.sin(PSI), X], [np.sin(PSI), np.cos(PSI), Y], [0, 0, 1]])
        R_i2r = np.linalg.inv(R_r2i)
        pi = np.matrix([[wp[0]], [wp[1]], [1]])
        pr = np.matmul(R_i2r, pi)
        lg = np.array([pr[0, 0], pr[1, 0]])
        return lg

    def global_path_callback(self, msg: Path):
        gp = []
        for pose in msg.poses:
            gp.append([pose.pose.position.x, pose.pose.position.y])
        if len(gp) == 0:
            return -1
        gp = np.array(gp)
        x = gp[:,0]
        try:
            xhat = scipy.signal.savgol_filter(x, 19, 3)
        except:
            xhat = x
        y = gp[:,1]
        try:
            yhat = scipy.signal.savgol_filter(y, 19, 3)
        except:
            yhat = y
        gphat = np.column_stack((xhat, yhat))
        gphat.tolist()
        self.global_path = gphat

    def get_obs(self):
        if self.laser_scan is None:
            print("laser is None")
            return None
        if self.global_path is None:
            print("path is None")
            return None
        laser_scan = np.array(self.laser_scan)
        laser_scan[laser_scan > 20] = 20
        laser_scan = (laser_scan - 20 / 2.) / 20 * 2 # scale to (-1, 1)

        goal = self.global_path[-1]  # Goal is the last point on the global path
        # transform the goal coordinates in robot's frame
        goal = self.transform_lg(goal, self.X, self.Y, self.PSI).reshape(-1) / 10.0

        # observation is laser_scan + goal coordinate
        return np.concatenate([laser_scan, goal])

def get_encoder(encoder_type, args):
    if encoder_type == "mlp":
        encoder=MLPEncoder(**args)
    elif encoder_type == 'rnn':
        encoder=RNNEncoder(**args)
    elif encoder_type == 'cnn':
        encoder=CNNEncoder(**args)
    elif encoder_type == 'transformer':
        encoder=TransformerEncoder(**args)
    elif encoder_type == 'hybrid':
        encoder=HybridEncoder(**args)
    elif encoder_type == 'hybrid_transformer':
        encoder=HybridTransformerEncoder(**args)
    else:
        raise Exception(f"[error] Unknown encoder type {encoder_type}!")
    return encoder

def init_policy(config_path):
    with open(os.path.join(config_path, "config.yaml"), 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    training_config = config["training_config"]

    state_dim = (724,)
    action_dim = 2
    action_space_low = np.array([-1, -3.14])
    action_space_high = np.array([2, 3.14])
    # devices = GPUtil.getAvailable(order = 'first', limit = 1, maxLoad = 0.8, maxMemory = 0.8, includeNan=False, excludeID=[], excludeUUID=[])
    device = "cpu"  # "cuda:%d" %(devices[0]) if len(devices) > 0 else "cpu"
    print("    >>>> Running on device %s" %(device))

    encoder_type = training_config["encoder"]
    encoder_args = {
        'input_dim': state_dim[-1],  # np.prod(state_dim),
        'num_layers': training_config['encoder_num_layers'],
        'hidden_size': training_config['encoder_hidden_layer_size'],
        'history_length': config["env_config"]["stack_frame"],
    }

    input_dim = training_config['hidden_layer_size']
    actor = Actor(
        #state_preprocess=state_preprocess,
        state_preprocess=get_encoder(encoder_type, encoder_args),
        head=MLP(input_dim, training_config['encoder_num_layers'], training_config['encoder_hidden_layer_size']),
        #head=nn.Identity(),
        action_dim=action_dim
    ).to(device)
    actor_optim = torch.optim.Adam(
        actor.parameters(), 
        lr=training_config['actor_lr']
    )
    print("Total number of parameters: %d" %sum(p.numel() for p in actor.parameters()))
    input_dim += np.prod(action_dim)
    critic = Critic(
        state_preprocess=get_encoder(encoder_type, encoder_args),
        head=MLP(input_dim, training_config['encoder_num_layers'], training_config['encoder_hidden_layer_size']),
        #head=nn.Identity(),
    ).to(device)
    critic_optim = torch.optim.Adam(
        critic.parameters(), 
        lr=training_config['critic_lr']
    )
    if training_config["dyna_style"]:
        model = Model(
            state_preprocess=get_encoder(encoder_type, encoder_args),
            head=MLP(input_dim, training_config['encoder_num_layers'], training_config['encoder_hidden_layer_size']),
            state_dim=state_dim,
            deterministic=training_config['deterministic']
        ).to(device)
        model_optim = torch.optim.Adam(
            model.parameters(), 
            lr=training_config['model_lr']
        )
        policy = DynaTD3(
            model, model_optim,
            training_config["model_update_per_step"],
            training_config["n_simulated_update"],
            actor, actor_optim,
            critic, critic_optim,
            action_range=[action_space_low, action_space_high],
            device=device,
            **training_config["policy_args"]
        )
    elif training_config["MPC"]:
        model = Model(
            state_preprocess=get_encoder(encoder_type, encoder_args),
            head=MLP(input_dim, training_config['encoder_num_layers'], training_config['encoder_hidden_layer_size']),
            state_dim=state_dim,
            deterministic=training_config['deterministic']
        ).to(device)
        model_optim = torch.optim.Adam(
            model.parameters(), 
            lr=training_config['model_lr']
        )
        policy = SMCPTD3(
            model, model_optim,
            training_config["horizon"],
            training_config["num_particle"],
            training_config["model_update_per_step"],
            actor, actor_optim,
            critic, critic_optim,
            action_range=[action_space_low, action_space_high],
            device=device,
            **training_config["policy_args"]
        )
    elif training_config["safe_rl"]:
        safe_critic = Critic(
            state_preprocess=get_encoder(encoder_type, encoder_args),
            head=MLP(input_dim, training_config['encoder_num_layers'], training_config['encoder_hidden_layer_size']),
        ).to(device)
        safe_critic_optim = torch.optim.Adam(
            safe_critic.parameters(), 
            lr=training_config['critic_lr']
        )
        policy = TD3(
            actor, actor_optim, 
            critic, critic_optim, 
            action_range=[action_space_low, action_space_high],
            safe_critic=safe_critic, safe_critic_optim=safe_critic_optim,
            device=device,
            safe_lagr=training_config['safe_lagr'],
            safe_mode=training_config['safe_mode'],
            **training_config["policy_args"]
        )
    else:
        policy = TD3(
            actor, actor_optim, 
            critic, critic_optim, 
            action_range=[action_space_low, action_space_high],
            device=device,
            **training_config["policy_args"]
        )

    policy.load(config_path, "last_policy")
    policy.exploration_noise = 0.0
    return policy

if __name__ == "__main__":
    FREQUENCY = 5.0  # In Hz

    rospy.init_node('step_recording', anonymous=True)
    rospy.set_param('/use_sim_time', True)

    step_recorder = StepRecorder()

    robot_state_sub = rospy.Subscriber(
        "/odometry/filtered",
        Odometry,
        step_recorder.robot_stat_callback,
        queue_size=1
    )
    laser_scan_sub = rospy.Subscriber(
        "/front/scan",
        LaserScan,
        step_recorder.laser_scan_callback,
        queue_size=1
    )
    global_path_sub = rospy.Subscriber(
        "/move_base/NavfnROS/plan",
        Path,
        step_recorder.global_path_callback,
        queue_size=1
    )
    cmd_vel_pub = rospy.Publisher('/cmd_vel', Twist, queue_size=1)

    ## Initialize your policy here
    policy = init_policy("models/0725/easy")
    act = np.zeros(2)
    hh = deque(maxlen=8)

    while step_recorder.get_obs() is None:
        time.sleep(0.1)
    obs = step_recorder.get_obs()
    obs = np.concatenate([obs, act])
    hh.extend([obs] * 8)

    time = rospy.get_time()
    while not rospy.is_shutdown():
        obs = step_recorder.get_obs()
        obs = np.concatenate([obs, act])
        print(obs[-4:-2] * 10, act)
        hh.append(obs)
        obs = np.stack(hh)
        act = policy.select_action(obs)
        v, w = act[0], act[1]
        
        cmd_vel_value = Twist()
        cmd_vel_value.linear.x = v
        cmd_vel_value.angular.z = w
        cmd_vel_pub.publish(cmd_vel_value)

        while rospy.get_time() - time < 1/FREQUENCY:
            rospy.sleep(0.1/FREQUENCY)

        time = rospy.get_time()
