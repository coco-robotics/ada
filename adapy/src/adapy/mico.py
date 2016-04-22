#!/usr/bin/env python

# Copyright (c) 2014, Carnegie Mellon University
# All rights reserved.
# Authors: Tekin Mericli <tekin@cmu.edu>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# - Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# - Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# - Neither the name of Carnegie Mellon University nor the names of its
#   contributors may be used to endorse or promote products derived from this
#   software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import openravepy
from prpy.base.manipulator import Manipulator
from std_msgs.msg import Float64
import rospy
import threading
from prpy.util import Watchdog

class Mico(Manipulator):
    def __init__(self, sim,
                 iktype=openravepy.IkParameterization.Type.Transform6D):
        Manipulator.__init__(self)

        self.simulated = sim
        self.iktype = iktype

        robot = self.GetRobot()
        env = robot.GetEnv()

        with env:
            dof_indices = self.GetIndices()
            accel_limits = robot.GetDOFAccelerationLimits()
            accel_limits[dof_indices[0]] = 1.45
            accel_limits[dof_indices[1]] = 1.56
            accel_limits[dof_indices[2]] = 1.56
            accel_limits[dof_indices[3]] = 1.5
            accel_limits[dof_indices[4]] = 1.48
            accel_limits[dof_indices[5]] = 1.49
            robot.SetDOFAccelerationLimits(accel_limits)

        # Load or_nlopt_ik as the IK solver. Unfortunately, IKFast doesn't work
        # on the Mico.
        if iktype is not None:
            self.iksolver = openravepy.RaveCreateIkSolver(env, 'TracIK')
            self.SetIKSolver(self.iksolver)

        # Load simulation controllers.
        if sim:
            from prpy.simulation import ServoSimulator

            self.controller = robot.AttachController(
                self.GetName(), '', self.GetArmIndices(), 0, True)
            self.servo_simulator = ServoSimulator(self, rate=20,
                                                  watchdog_timeout=0.1)
        else:
            #if not simulation, create publishers for each joint
            self.velocity_topic_names = ['vel_j'+str(i)+'_controller/command' for i in range(1,7)]
            self.velocity_publishers = [rospy.Publisher(topic_name, Float64, queue_size=1) for topic_name in self.velocity_topic_names]
            self.velocity_publisher_lock = threading.Lock()
            
            #create watchdog to send zero velocity
            self.servo_watchdog = Watchdog(timeout_duration=0.25, handler=self.SendVelocitiesToMico, args=[[0.,0.,0.,0.,0.,0.,]])

    def CloneBindings(self, parent):
        super(Mico, self).CloneBindings(parent)

        self.simulated = True
        self.iktype = parent.iktype

        self.servo_simulator = None

        # TODO: This is broken on nlopt_ik
        """
        if parent.iktype is not None:
            self.iksolver = openravepy.RaveCreateIkSolver(env, 'NloptIK')
            self.SetIKSolver(self.iksolver)
        """

    def Servo(self, velocities):
        """
        Servo with an instantaneous vector of joint velocities.
        @param velocities instantaneous joint velocities in radians per second
        """
        num_dof = len(self.GetArmIndices())

        if len(velocities) != num_dof:
            raise ValueError(
                'Incorrect number of joint velocities.'
                ' Expected {:d}; got {:d}.'.format(num_dof, len(velocities)))

        if self.simulated:
            self.GetRobot().GetController().Reset(0)
            self.servo_simulator.SetVelocity(velocities)
        else:
            self.SendVelocitiesToMico(velocities)
            #reset watchdog timer
            self.servo_watchdog.reset()


    def SendVelocitiesToMico(self, velocities):
        with self.velocity_publisher_lock:
            for velocity_publisher,velocity in zip(self.velocity_publishers, velocities):
                velocity_publisher.publish(velocity)
