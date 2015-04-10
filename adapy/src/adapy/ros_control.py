import logging


class ControlException(RuntimeError):
    pass


class InternalError(ControlException):
    pass


class TrajectoryExecutionFailed(ControlException):
    def __init__(self, message, requested, executed):
        super(TrajectoryExecutionFailed, self).__init__(message)

        self.requested = requested
        self.executed = executed

class TimeoutError(Exception):
    pass

class CancelledError(Exception):
    pass

class Future(object):
    logger = logging.getLogger('future')

    def __init__(self):
        from Queue import Queue
        from threading import Condition, RLock

        self.lock = RLock()

        self._is_done = False
        self._is_error = False
        self._is_cancelled = False

        self._handle = None
        self._result = None

        self._condition = Condition(self.lock)
        self._callbacks = []

    def done(self):
        """ Return True if the call was cancelled or finished running. """
        with self.lock:
            return self._is_done

    def cancel(self):
        """ Attempt to cancel the call. """
        raise NotImplementedError('Cancelling is not supported.')

    def cancelled(self):
        """ Return True if the call was successfully cancelled. """
        with self.lock:
            return self._is_done and self._is_cancelled

    def result(self, timeout=None):
        """ Return the value returned by the call.

        If the call hasn’t yet completed then this method will wait up to
        timeout seconds. If the call hasn’t completed in timeout seconds, then
        a TimeoutError will be raised. timeout can be an int or float. If
        timeout is not specified or None, there is no limit to the wait time.

        If the future is cancelled before completing then CancelledError will
        be raised.

        If the call raised, this method will raise the same exception.
        """
        with self.lock:
            self._condition.wait(timeout)

            if not self._is_done:
                raise TimeoutError()
            elif self._is_cancelled:
                raise CancelledError()
            elif self._exception is not None:
                raise self._exception
            else:
                return self._result

    def exception(self, timeout=None):
        """ Return the exception raised by the call.

        If the call hasn’t yet completed then this method will wait up to
        timeout seconds. If the call hasn’t completed in timeout seconds, then
        a TimeoutError will be raised. timeout can be an int or float. If
        timeout is not specified or None, there is no limit to the wait time.

        If the future is cancelled before completing then CancelledError will
        be raised.

        If the call completed without raising, None is returned.
        """
        with self.lock:
            self._condition.wait(timeout)

            if not self._is_done:
                raise TimeoutError()
            elif self._is_cancelled:
                raise CancelledError()
            elif self._exception is not None:
                return self._exception
            else:
                return None

    def add_done_callback(self, fn):
        """ Attaches the callable fn to the future.

        fn will be called, with the future as its only argument, when the
        future is cancelled or finishes running. If fn was already added as a
        callback, this will raise an InternalError.

        Added callables are called in the order that they were added and are
        always called in a thread belonging to the process that added them. If
        the callable raises a Exception subclass, it will be logged and
        ignored. If the callable raises a BaseException subclass, the behavior
        is undefined.

        If the future has already completed or been cancelled, fn will be
        called immediately.
        """
        with self.lock:
            if self._is_done:
                if fn in self._callbacks:
                    raise InternalError('Callback is already registered.')

                self._callbacks.append(fn)
                do_call = False
            else:
                do_call = True

        if do_call:
            fn(self)

    def remove_done_callback(self, fn):
        """ Removes the callable fn to the future.

        If fn is not registered as a callback, this will raise an Exception.
        """
        with self.lock:
            try:
                self._callbacks.remove(fn)
            except ValueError:
                raise InternalError('Callback was not registered.')

    def set_result(self, result):
        """ Set the result of this Future. """
        self._result = result
        self._set_done()

    def set_cancelled(self):
        """ Flag this Future as being cancelled. """
        self._is_cancelled = True
        self._set_done()

    def set_exception(self, exception):
        """ Indicates that an exception has occurred. """
        self._exception = exception
        self._set_done()

    def _set_done(self):
        """ Mark this future as done and return a callback function.
        """
        with self.lock:
            if self._is_done:
                raise InternalError('This future is already done.')

            self._is_done = True
            callbacks = list(self._callbacks)

            self._condition.notify_all()

        for callback_fn in callbacks:
            try:
                callback_fn(self)
            except Exception as e:
                self.logger.exception('Callback raised an exception.')


class TrajectoryFuture(Future):
    def __init__(self, traj_requested):
        from actionlib import CommState
        from copy import deepcopy
        from trajectory_msgs.msg import JointTrajectory

        self._prev_state = CommState.PENDING
        self._traj_requested = deepcopy(traj_requested)
        self._traj_executed = JointTrajectory(
            joint_names=traj_requested.joint_names
        )

    def requested(self):
        from copy import deepcopy

        return deepcopy(self._traj_requested)

    def partial_result(self):
        from copy import deepcopy

        with self.lock:
            return deepcopy(self._traj_executed)

    def _on_done(self, terminal_state, result):
        from actionlib import TerminalState, get_name_of_constant
        from copy import deepcopy
        from control_msgs.msg import FollowJointTrajectoryResult

        if terminal_state == TerminalState.SUCCEEDED:
            # Trajectory execution succeeded. Return the trajectory.
            if result.error_code == FollowJointTrajectoryResult.SUCCESSFUL:
                self.set_result(self._traj_executed)
            # Trajectory execution failed. Raise an exception.
            else:
                self.set_exception(
                    TrajectoryExecutionFailed(
                        'Trajectory execution failed ({:s}): {:s}'.format(
                            get_name_of_constant(FollowJointTrajectoryResult,
                                                 result.error_code),
                            result.error_string
                        ),
                        executed=partial_result(),
                        requested=deepcopy(self._traj_requested)

                    )
                )
        # Goal was cancelled. Note that this could have been one by another
        # thread or process, so _cancelled may be False.
        elif terminal_state not in [TerminalState.PREEMPTED,
                                    TerminalState.RECALLED]:
            self.set_cancelled()
        else:
            self.set_exception(
                TrajectoryExecutionFailed(
                    'Trajectory execution failed ({:s}): {:s}'.format(
                        get_name_of_constant(TerminalState, terminal_state),
                        self._handle.get_goal_status_text()
                    ),
                    executed=partial_result(),
                    requested=deepcopy(self._traj_requested)
                )
            )

    def on_transition(self, handle):
        from actionlib import CommState

        state = handle.get_state()

        # Transition to the "done" state. This occurs when the trajectory
        # finishes for any reason (including an error).
        if state == self._prev_state:
            pass
        elif state == CommState.DONE:
            self._on_done(handle.get_terminal_state(), handle.get_result())

        self._prev_state = state

    def on_feedback(self, msg):
        with self.lock:
            if not self._traj_executed.header.stamp:
                self._traj_executed.header.stamp = (msg.header.stamp
                                                  - msg.actual.time_from_start)

            self._traj_executed.points.append(msg.actual)


class FollowJointTrajectoryClient(object):
    def __init__(self, ns):
        from actionlib import ActionClient
        from control_msgs.msg import FollowJointTrajectoryAction

        self._client = ActionClient(ns, FollowJointTrajectoryAction)
        self._client.wait_for_server()

    def execute(self, traj_msg):
        from control_msgs.msg import FollowJointTrajectoryActionGoal 
        import control_msgs.msg

        traj_future = TrajectoryFuture(traj_msg)
        traj_future._handle = self._client.send_goal(
            FollowJointTrajectoryActionGoal(trajectory=traj_msg),
            transition_cb=traj_future.on_transition,
            feedback_cb=traj_future.on_feedback
        )
        return traj_future


class TrajectoryMode(ROSControlMode):
    def __init__(self, ns):
        from actionlib import ActionClient
        from control_msgs.msg import JointTrajectoryAction
        from threading import Lock

        self._lock = Lock()
        self._queue = []

        self._client = ActionClient(ns, JointTrajectoryAction)
        self._client.wait_for_server()

    def running(self):
        with self._lock:
            return bool(self._queue)

    def execute_ros_trajectory(self, traj_msg):
        from control_msgs.msg import JointTrajectoryActionGoal

        goal_msg = JointTrajectoryActionGoal(
            trajectory=traj_msg,
            path_tolerance=[], # use default values
            goal_tolerance=[], # use default values
            goal_time_tolerance=0. # use default value
        )

        # Return a TrajectoryFuture to track execution state.
        traj_future = TrajectoryFuture()
        traj_future._handle = self._client.send_goal(
            goal_msg,
            transition_cb=traj_future._transition_callback,
            feedback_cb=traj_future._feedback_callback
        )

        # Add this trajectory to the queue of running trajectories. Remove it
        # when it finishes executing.
        with self._lock:
            self._queue.append(traj_future)

        def remove_from_queue(_):
            with self._lock:
                self._queue.remove(traj_future)

        traj_future.add_done_callback(remove_from_queue)

        return traj_future
