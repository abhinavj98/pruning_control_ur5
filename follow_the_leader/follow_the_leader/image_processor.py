#!/usr/bin/env python3
import rclpy
import numpy as np
from sensor_msgs.msg import Image, CameraInfo
from geometry_msgs.msg import Vector3
from follow_the_leader_msgs.msg import ImageMaskPair, StateTransition

from cv_bridge import CvBridge
from follow_the_leader.utils.ros_utils import TFNode, process_list_as_dict
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.parameter import Parameter
from threading import Lock
from scipy.spatial.transform import Rotation
#Publishes the optical flow-based foreground segmentations
#TODO: Replace with raft model

bridge = CvBridge()


class ImageProcessorNode(TFNode):
    def __init__(self):
        super().__init__("image_processor_node", cam_info_topic="/camera/color/camera_info")

        # ROS2 params
        self.movement_threshold = self.declare_parameter("movement_threshold", 0.0075)
        self.segmentation_model_name = self.declare_parameter("segmentation_model_name", "YOLO")
        self.base_frame = self.declare_parameter("base_frame", "base_link")
        self.camera_topic_name = self.declare_parameter("camera_topic_name", '/camera/color/image_raw') #TODO: Change to Parameter.Type.STRING

        # State variables
        self.image_processor = None
        self.just_activated = False
        self.last_image = None
        self.last_pose = None
        self.last_skipped = False

        # ROS2 setup
        self.lock = Lock()
        self.cb = MutuallyExclusiveCallbackGroup()
        self.cb_reentrant = ReentrantCallbackGroup()
        self.pub = self.create_publisher(Image, "image_mask", 10)
        self.image_mask_pub = self.create_publisher(ImageMaskPair, "image_mask_pair", 10)
        self.sub = self.create_subscription(
            Image,
            self.camera_topic_name.get_parameter_value().string_value,
            self.image_callback,
            1,
            callback_group=self.cb,
        )
        self.transition_sub = self.create_subscription(
            StateTransition, "state_transition", self.handle_state_transition, 1, callback_group=self.cb_reentrant
        )
        # self.switch_controller_srv = self.create_service(
        #     Trigger, "await_resource_ready", self.await_resource_ready, callback_group=self.cb
        # )
        return

    def load_image_processor(self, force_size=None):
        with self.lock:
            if self.image_processor is None and (self.camera.tf_frame or force_size):
                if self.camera.tf_frame:
                    size = (self.camera.width, self.camera.height)
                else:
                    size = force_size
                segmentation_model_name = self.segmentation_model_name.get_parameter_value().string_value
                print("Loading segmentation model {}".format(segmentation_model_name))
                if segmentation_model_name == "YOLO":
                    from follow_the_leader.networks.yolov8 import YoloInference
                    print("Loading YOLO model")
                    self.image_processor = YoloInference(input_size=size, output_size=size)
                elif segmentation_model_name == "FlowGAN":
                    from follow_the_leader.networks.flowgan import FlowGAN
                    self.image_processor = FlowGAN(
                        size,
                        size,
                        use_flow=True,
                        gan_name="synthetic_flow_pix2pix",
                        gan_input_channels=6,
                        gan_output_channels=1,
                    )
                else:
                    raise ValueError("Unknown segmentation model {}".format(segmentation_model_name))
        return

    def _handle_cam_info(self, msg: CameraInfo):
        super()._handle_cam_info(msg)
        self.load_image_processor()
        return

    def handle_state_transition(self, msg: StateTransition):
        action = process_list_as_dict(msg.actions, "node", "action").get(self.get_name())
        if not action:
            return

        if action == "activate":
            pass
        elif action == "reset":
            self.reset()
        else:
            raise ValueError("Unknown action {} for node {}".format(action, self.get_name()))
        return

    def reset(self, reset_pose=True):
        if self.image_processor is not None:
            self.image_processor.reset()
        self.just_activated = True
        self.last_image = None
        self.last_skipped = False
        if reset_pose:
            self.last_pose = None
        return

    def image_callback(self, msg: Image):
        # self.last_image = msg
        if self.image_processor is None:
            return
        vec = Vector3()
        segmentation_model_name = self.segmentation_model_name.get_parameter_value().string_value
        if segmentation_model_name == "FlowGAN":
            """Need to compute the optical flow between the current and previous image for flowgan
             This checks if the optical flow is nice"""
            if self.movement_threshold.value:
                tf_mat = self.lookup_transform(
                    self.base_frame.value, self.camera.tf_frame, rclpy.time.Time(), as_matrix=True
                )
                pos = tf_mat[:3, 3]
                if self.last_pose is None:
                    self.last_pose = tf_mat
                else:
                    # If the camera has rotated too much, we assume we get bad optical flows
                    rotation = Rotation.from_matrix(self.last_pose[:3, :3].T @ tf_mat[:3, :3]).as_euler("XYZ")
                    if np.linalg.norm(rotation) > np.radians(0.5):
                        self.last_pose = tf_mat
                        self.last_skipped = True
                        return

                    last_pos = self.last_pose[:3, 3]
                    diff = pos - last_pos
                    if np.linalg.norm(diff) < self.movement_threshold.value:
                        return

                    movement = np.linalg.inv(tf_mat[:3, :3]) @ diff
                    movement /= np.linalg.norm(movement)
                    vec = Vector3(x=movement[0], y=movement[1], z=movement[2])
                    self.last_pose = tf_mat

        img = bridge.imgmsg_to_cv2(msg, desired_encoding="rgb8")
        if segmentation_model_name == "FlowGAN":
            mask = self.image_processor.process(img).mean(axis=2).astype(np.uint8)

        if segmentation_model_name == "YOLO":
            mask = self.image_processor.process(img).astype(np.uint8)#.mean(axis=2).astype(np.uint8)
        if self.just_activated:
            self.just_activated = False
            return

        if self.last_skipped:
            self.last_skipped = False
            return
        mask_msg = bridge.cv2_to_imgmsg(mask, encoding="mono8")
        mask_msg.header.stamp = msg.header.stamp
        image_mask_pair = ImageMaskPair(rgb=msg, mask=mask_msg, image_frame_offset=vec)

        self.pub.publish(mask_msg)
        self.image_mask_pub.publish(image_mask_pair)
        return

    def switch_segmentation_model_callback(self, segmentation_model_name):
        self.segmentation_model_name.set_parameter_value(Parameter.Type.STRING, segmentation_model_name)
        self.load_image_processor()
        return

def main(args=None):
    rclpy.init(args=args)
    executor = MultiThreadedExecutor()
    node = ImageProcessorNode()
    rclpy.spin(node, executor=executor)
    return


if __name__ == "__main__":
    main()
