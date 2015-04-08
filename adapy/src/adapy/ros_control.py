import logging


class ControlException(RuntimeError):
    pass


class InternalError(ControlException):
    pass


class TrajectoryExecutionFailed(ControlException):
    pass


def condition_wait(condition, timeout, test_fn):
    """ Wait for a condition variable with a timeout.

    The default Condition.wait() function has two major limitations:
    
    First, it subject to spurious wakeups. As a result, it must be called in a
    "while" loop that checks whether the condition is satisfied. This makes it
    difficult to use the "timeout" parameter without additional bookkeeping.

    Second, there is no way to tell whether a timeout has occurred. We provide
    a simple (and approximate) solution by returning True if test_fn() returned
    True and False otherwise. Note that this test is approximate.

    @param condition condition variable to wait for
    @param timeout maximum time to wait, in seconds
    @param test_fn boolean termination condition
    @return boolean indicating whether test_fn() returned True
    """
    import time

    time_now = time.time()
    time_done = time_now + timeout
    result = test_fn()

    while not result and time_now < time_done:
        condition.wait(time_now - time_done)
        result = test_fn()
        time_now = time.time()

    return result


class TrajectoryFuture(object):
    logger = logging.getLogger('TrajectoryFuture')

    def __init__(self):
        from trajectory_msgs.msg import JointTrajectory
        from threading import Condition, Lock

        self._lock = Lock()
        self._handle = None

        # Flags:
        # - _cancelled: set if execution is cancelled (even if not by us)
        # - _done: set when execution terminates, regardless of the cause
        #
        # When _done is set to True, the _done_condition variable is notified
        # and the functions in _done_callbacks are called sequentially.
        self._cancelled = False
        self._done = False
        self._done_condition = Condition(self._lock)
        self._done_callbacks = []

        # Result variables:
        # - _result: set if execution succeeds
        # - _exception: set if an error occurrs.
        # 
        # Both of values remain None if execution is cancelled.
        self._result = None
        self._exception = None

        self._traj_actual = JointTrajectory()

    def cancel(self):
        with self._lock:
            if self._handle is None:
                raise InternalError('This TrajectoryFuture is not initialized.')
            elif self._cancelled:
                return True
            elif self._done:
                return False

            self._handle.cancel()
            return True

    def cancelled(self):
        with self._lock:
            return self._done and self._cancelled

    def running(self):
        with self._lock:
            return not self._done

    def done(self):
        with self._lock:
            return self._done

    def result(self, timeout=None):
        from concurrent.futures import CancelledError, TimeoutError

        with self._done_condition:
            condition_wait(self._done_condition, timeout, lambda: self._done)

            if not self._done:
                raise TimeoutError()
            elif self._cancelled:
                raise CancelledError()
            elif self._exception is not None:
                raise self._exception
            else:
                return self._result

    def partial_result(self):
        from copy import deepcopy

        with self._lock:
            return deepcopy(self._traj_actual)

    def exception(self, timeout=None):
        from concurrent.futures import CancelledError

        with self._done_condition:
            condition_wait(self._done_condition, timeout, lambda: self._done)

            if not self._done:
                raise TimeoutError()
            elif self._cancelled:
                raise CancelledError()
            elif self._exception is not None:
                return self._exception
            else:
                return None

    def add_done_callback(self, fn):
        with self._lock:
            if self._done:
                self._call_callback(fn)
            else:
                self._done_callbacks.append(fn)

    def _set_done(self, terminal_state, result):
        # NOTE: This function MUST be called with the lock acquired.

        from actionlib import TerminalState, get_name_of_constant
        from control_msgs.msg import FollowJointTrajectoryResult

        # The actionlib call succeeded, so "result" is valid.
        if terminal_state == TerminalState.SUCCEEDED:
            Result = FollowJointTrajectoryResult

            # Trajectory execution succeeded. Return the trajectory.
            if result.error_code == Result.SUCCESSFUL:
                self._result = self._traj_actual
            # Trajectory execution failed. Raise an exception.
            else:
                self._exception = TrajectoryExecutionFailed(
                    'Trajectory execution failed ({:s}): {:s}'.format(
                        get_name_of_constant(Result, result.error_code),
                        result.error_string))
        # Goal was cancelled. Note that this could have been one by another
        # thread or process, so _cancelled may be False.
        elif terminal_state not in [TerminalState.PREEMPTED,
                                    TerminalState.RECALLED]:
            self._cancelled = True
        else:
            self._exception = TrajectoryExecutionFailed(
                'Trajectory execution failed ({:s}): {:s}'.format(
                    get_name_of_constant(TerminalState, terminal_state),
                    self._handle.get_goal_status_text()))

        # Flag this future as "done".
        self._done = True
        self._done_condition.notify_all()

    def _call_callback(self, fn):
        try:
            fn(self._result)
        except Exception as e:
            self.logger.exception('Callback raised an exception.')

    def _transition_callback(self, handle):
        from actionlib import CommState

        state = handle.get_state()
        do_callbacks = False

        # Transition to the "done" state. This occurs when the trajectory
        # finishes for any reason (including an error).
        with self._lock:
            if not self._done and state == CommState.DONE:
                self._set_done(handle.get_terminal_state(),
                               handle.get_result())
                do_callbacks = True

        # Call any registered "done" callbacks. We intentionally do this
        # outside of _set_done so we can release the lock.
        if do_callbacks:
            for fn in self._done_callbacks:
                self._call_callback(fn)

    def _feedback_callback(self, feedback_msg):
        with self._lock:
            # Initialize the trajectory's start time with the timestamp of the
            # first observed feedback message.
            if not self._traj_actual.header.stamp:
                self._traj_actual.header.stamp = feedback_msg.header.stamp

            actual_waypoint = feedback_msg.actual
            actual_waypoint.time_from_start = feedback_msg.header.stamp \
                                            - self._traj_actual.header.stamp
            self._traj_actual.points.append(actual_waypoint)


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
