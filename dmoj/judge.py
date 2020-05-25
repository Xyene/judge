#!/usr/bin/python
import logging
import os
import queue
import signal
import sys
import multiprocessing
import threading
import traceback
from enum import Enum
from http.server import HTTPServer
from itertools import groupby
from operator import itemgetter
from typing import Any, Callable, Dict, Generator, List, NamedTuple, Optional, Tuple, Union

from dmoj import packet
from dmoj.control import JudgeControlRequestHandler
from dmoj.error import CompileError
from dmoj.judgeenv import clear_problem_dirs_cache, env, get_supported_problems, startup_warnings
from dmoj.monitor import Monitor
from dmoj.problem import BatchedTestCase, Problem, TestCase
from dmoj.result import Result
from dmoj.utils.ansi import ansi_style, print_ansi, strip_ansi
from dmoj.utils.unicode import unicode_stdout_stderr, utf8bytes, utf8text

try:
    import readline  # noqa: F401, imported for the side-effect of making `input()` have readline.
except ImportError:
    pass

try:
    from setproctitle import setproctitle
except ImportError:

    def setproctitle(title):
        pass


class IPC(Enum):
    HELLO = 'HELLO'
    BYE = 'BYE'
    COMPILE_ERROR = 'COMPILE-ERROR'
    COMPILE_MESSAGE = 'COMPILE-MESSAGE'
    RESULT = 'RESULT'
    BATCH_BEGIN = 'BATCH-BEGIN'
    BATCH_END = 'BATCH-END'
    GRADING_BEGIN = 'GRADING-BEGIN'
    GRADING_END = 'GRADING-END'
    GRADING_ABORTED = 'GRADING-ABORTED'
    UNHANDLED_EXCEPTION = 'UNHANDLED-EXCEPTION'
    REQUEST_ABORT = 'REQUEST-ABORT'


IPC_TEARDOWN_TIMEOUT = 5  # seconds


logger = logging.getLogger(__name__)


Submission = NamedTuple(
    'Submission',
    [
        ('id', int),
        ('problem_id', str),
        ('language', str),
        ('source', str),
        ('time_limit', float),
        ('memory_limit', int),
        ('short_circuit', bool),
        ('meta', Dict),
    ],
)


class Judge:
    def __init__(self, packet_manager: packet.PacketManager) -> None:
        self.packet_manager = packet_manager
        # FIXME(tbrindus): marked as Any since PacketManager likes querying current_submission.id directly.
        self.current_submission: Any = None
        self.current_judge_worker: Optional[JudgeWorker] = None

        self.action_queue: queue.Queue = queue.Queue()

        def _action_thread_main():
            while True:
                action = self.action_queue.get()
                if not action:
                    # Treat a None action as the signal that the judge is exiting.
                    return

                action()

        self.action_thread = threading.Thread(target=_action_thread_main)

    def update_problems(self):
        """
        Pushes current problem set to server.
        """

        def _action_update_problems():
            log = logging.getLogger('dmoj.updater')
            try:
                clear_problem_dirs_cache()
                self.packet_manager.supported_problems_packet(get_supported_problems())
            except Exception:
                log.exception('Failed to update problems.')

        self.action_queue.put(_action_update_problems)

    def begin_grading(self, submission: Submission, report=logger.info, blocking=False) -> None:
        ipc_ready_signal = threading.Event()
        grading_thread = threading.Thread(
            target=self._grading_thread_main, args=(ipc_ready_signal, submission, report), daemon=True
        )

        def _action_begin_grading():
            self.current_submission = submission
            grading_thread.start()
            ipc_ready_signal.wait()

        if blocking:
            _action_begin_grading()
            grading_thread.join()
        else:
            self.action_queue.put(_action_begin_grading)

    def _grading_thread_main(self, ipc_ready_signal: threading.Event, submission: Submission, report) -> None:
        try:
            report(
                ansi_style(
                    'Start grading #ansi[%s](yellow)/#ansi[%s](green|bold) in %s...'
                    % (submission.problem_id, submission.id, submission.language)
                )
            )

            self.current_judge_worker = JudgeWorker(submission)

            ipc_handler_dispatch: Dict[IPC, Callable] = {
                IPC.HELLO: lambda _report: ipc_ready_signal.set(),
                IPC.COMPILE_ERROR: self._ipc_compile_error,
                IPC.COMPILE_MESSAGE: self._ipc_compile_message,
                IPC.GRADING_BEGIN: self._ipc_grading_begin,
                IPC.GRADING_END: self._ipc_grading_end,
                IPC.GRADING_ABORTED: self._ipc_grading_aborted,
                IPC.BATCH_BEGIN: self._ipc_batch_begin,
                IPC.BATCH_END: self._ipc_batch_end,
                IPC.RESULT: self._ipc_result,
                IPC.UNHANDLED_EXCEPTION: self._ipc_unhandled_exception,
            }

            for ipc_type, data in self.current_judge_worker.communicate():
                try:
                    handler_func = ipc_handler_dispatch[ipc_type]
                except KeyError:
                    raise RuntimeError(
                        "judge got unexpected IPC message from worker: %s" % ((ipc_type, data),)
                    ) from None

                handler_func(report, *data)

            report(
                ansi_style(
                    'Done grading #ansi[%s](yellow)/#ansi[%s](green|bold).\n' % (submission.problem_id, submission.id)
                )
            )
        except:  # noqa: E722, we want to catch everything
            self.log_internal_error()
        finally:
            if self.current_judge_worker is not None:
                self.current_judge_worker.stop()

            self.current_submission = None
            self.current_judge_worker = None

    def _ipc_compile_error(self, report, error_message: str) -> None:
        report(ansi_style('#ansi[Failed compiling submission!](red|bold)'))
        report(error_message.rstrip())  # don't print extra newline
        self.packet_manager.compile_error_packet(error_message)

    def _ipc_compile_message(self, _report, compile_message: str) -> None:
        self.packet_manager.compile_message_packet(compile_message)

    def _ipc_grading_begin(self, _report, is_pretested: bool) -> None:
        self.packet_manager.begin_grading_packet(is_pretested)

    def _ipc_grading_end(self, _report) -> None:
        self.packet_manager.grading_end_packet()

    def _ipc_result(self, report, batch_number: Optional[int], case_number: int, result: Result) -> None:
        codes = result.readable_codes()

        is_sc = result.result_flag & Result.SC
        colored_codes = ['#ansi[%s](%s|bold)' % ('--' if x == 'SC' else x, Result.COLORS_BYID[x]) for x in codes]
        colored_aux_codes = '{%s}' % ', '.join(colored_codes[1:]) if len(codes) > 1 else ''
        colored_feedback = '(#ansi[%s](|underline)) ' % utf8text(result.feedback) if result.feedback else ''
        if is_sc:
            case_info = ''
        else:
            case_info = '[%.3fs (%.3fs) | %dkb] %s%s' % (
                result.execution_time,
                result.wall_clock_time,
                result.max_memory,
                colored_feedback,
                colored_aux_codes,
            )
        case_padding = '  ' if batch_number is not None else ''
        report(ansi_style('%sTest case %2d %-3s %s' % (case_padding, case_number, colored_codes[0], case_info)))
        self.packet_manager.test_case_status_packet(case_number, result)

    def _ipc_batch_begin(self, report, batch_number: int) -> None:
        self.packet_manager.batch_begin_packet()
        report(ansi_style("#ansi[Batch #%d](yellow|bold)" % batch_number))

    def _ipc_batch_end(self, _report, _batch_number: int) -> None:
        self.packet_manager.batch_end_packet()

    def _ipc_grading_aborted(self, report) -> None:
        self.packet_manager.submission_aborted_packet()
        report(ansi_style('#ansi[Forcefully terminating grading. Temporary files may not be deleted.](red|bold)'))

    def _ipc_unhandled_exception(self, _report, message: str) -> None:
        logger.error("Unhandled exception in worker process")
        self.log_internal_error(message=message)

    def abort_grading(self) -> None:
        """
        Forcefully terminates the current submission. Not necessarily safe.
        """
        # Grab a local copy since it might get set to None after we do our None check.
        worker = self.current_judge_worker
        if worker:
            worker.abort_grading()

    def listen(self) -> None:
        """
        Attempts to connect to the handler server specified in command line.
        """
        self.packet_manager.run()

    def murder(self) -> None:
        """
        End any submission currently executing, and exit the judge.
        """
        self.abort_grading()
        self.action_queue.put(None)
        if self.packet_manager:
            self.packet_manager.close()

    def log_internal_error(self, exc: BaseException = None, message: str = None) -> None:
        if not message:
            # If exc exists, raise it so that sys.exc_info() is populated with its data.
            if exc:
                try:
                    raise exc
                except KeyboardInterrupt:
                    # Let KeyboardInterrupt bubble up.
                    raise
                except:  # noqa: E722, we want to catch everything
                    pass

            message = ''.join(traceback.format_exception(*sys.exc_info()))

        # Strip ANSI from the message, since this might be a checker's CompileError ...we don't want to see the raw ANSI
        # codes from GCC/Clang on the site. We could use format_ansi and send HTML to the site, but the site doesn't
        # presently support HTML internal error formatting.
        self.packet_manager.internal_error_packet(strip_ansi(message))

        # Logs can contain ANSI, and it'll display fine
        print(message, file=sys.stderr)


class JudgeWorker:
    def __init__(self, submission: Submission) -> None:
        self.submission = submission
        self._abort_requested = False
        # FIXME(tbrindus): marked Any pending grader cleanups.
        self.grader: Any = None

        self.worker_process_conn, child_conn = multiprocessing.Pipe()
        self.worker_process = multiprocessing.Process(
            name="DMOJ Judge Handler for %s/%d" % (self.submission.problem_id, self.submission.id),
            target=self._worker_process_main,
            args=(child_conn, self.worker_process_conn),
        )
        self.worker_process.start()
        child_conn.close()

    def communicate(self) -> Generator[Tuple[IPC, tuple], None, None]:
        while True:
            try:
                ipc_type, data = self.worker_process_conn.recv()
            except Exception:
                logger.error("Failed to read IPC message from worker!")
                raise

            if ipc_type == IPC.BYE:
                self.worker_process_conn.send((IPC.BYE, ()))
                return
            else:
                yield ipc_type, data

    def stop(self) -> None:
        if self.worker_process and self.worker_process.is_alive():
            # Might be None if run was never called, or failed.
            try:
                self.worker_process.join(timeout=IPC_TEARDOWN_TIMEOUT)
            except OSError:
                logger.exception('Exception while waiting for worker to shut down, ignoring...')
            finally:
                if self.worker_process.is_alive():
                    logger.error("Worker is still alive, sending SIGKILL!")
                    self.worker_process.kill()

    def abort_grading(self) -> None:
        assert self.worker_process_conn

        try:
            self.worker_process_conn.send((IPC.REQUEST_ABORT, ()))
        except Exception:
            logger.exception("Failed to send abort request to worker, did it race?")

        self.stop()

    def _worker_process_main(
        self,
        judge_process_conn: 'multiprocessing.connection.Connection',
        worker_process_conn: 'multiprocessing.connection.Connection',
    ) -> None:
        """
        Main body of judge worker process, which handles grading and sends grading results to the judge controller via
        IPC.
        """
        worker_process_conn.close()
        setproctitle(multiprocessing.current_process().name)

        def _ipc_recv_thread_main() -> None:
            """
            Worker thread that listens for incoming IPC messages from the judge controller.
            """
            while True:
                try:
                    ipc_type, data = judge_process_conn.recv()
                except:  # noqa: E722, whatever happened, we have to abort now.
                    logger.exception("Judge unexpectedly hung up!")
                    self._do_abort()
                    return

                if ipc_type == IPC.BYE:
                    return
                elif ipc_type == IPC.REQUEST_ABORT:
                    self._do_abort()
                else:
                    raise RuntimeError("worker got unexpected IPC message from judge: %s" % ((ipc_type, data),))

        ipc_recv_thread = None
        try:
            judge_process_conn.send((IPC.HELLO, ()))

            ipc_recv_thread = threading.Thread(target=_ipc_recv_thread_main, daemon=True)
            ipc_recv_thread.start()

            for ipc_msg in self._grade_cases():
                judge_process_conn.send(ipc_msg)

            judge_process_conn.send((IPC.BYE, ()))
        except BrokenPipeError:
            # There's nothing we can do about this... the general except branch would just fail again. Just re-raise and
            # hope for the best.
            raise
        except:  # noqa: E722, we explicitly want to notify the parent of everything
            # We can't pickle the whole traceback object, so just send the formatted exception.
            message = ''.join(traceback.format_exception(*sys.exc_info()))
            judge_process_conn.send((IPC.UNHANDLED_EXCEPTION, (message,)))
            judge_process_conn.send((IPC.BYE, ()))
        finally:
            if ipc_recv_thread is not None:
                # We may have failed before sending the IPC.BYE down the connection, in which case the judge will never
                # close its side of the connection -- so `ipc_recv_thread` will never exit. But we can't wait forever in
                # this case, since we're blocking the main judge from proceeding.
                ipc_recv_thread.join(timeout=IPC_TEARDOWN_TIMEOUT)
                if ipc_recv_thread.is_alive():
                    logger.error("Judge IPC recv thread is still alive after timeout, shutting worker down anyway!")

            # FIXME(tbrindus): we need to do this because cleaning up temporary directories happens on __del__, which
            # won't get called if we exit the process right now (so we'd leak all files created by the grader). This
            # should be refactored to have an explicit `cleanup()` or similar, rather than relying on refcounting
            # working out.
            self.grader = None

    def _grade_cases(self) -> Generator[Tuple[IPC, tuple], None, None]:
        problem = Problem(
            self.submission.problem_id, self.submission.time_limit, self.submission.memory_limit, self.submission.meta
        )

        try:
            self.grader = problem.grader_class(
                self, problem, self.submission.language, utf8bytes(self.submission.source)
            )
        except CompileError as compilation_error:
            error = compilation_error.args[0] or b'compiler exited abnormally'
            yield IPC.COMPILE_ERROR, (error,)
            return
        else:
            binary = self.grader.binary
            if hasattr(binary, 'warning') and binary.warning is not None:
                yield IPC.COMPILE_MESSAGE, (binary.warning,)

        yield IPC.GRADING_BEGIN, (self.grader.is_pretested,)

        flattened_cases: List[Tuple[Optional[int], Union[TestCase, BatchedTestCase]]] = []
        batch_number = 0
        for case in self.grader.cases():
            if isinstance(case, BatchedTestCase):
                batch_number += 1
                for batched_case in case.batched_cases:
                    flattened_cases.append((batch_number, batched_case))
            else:
                flattened_cases.append((None, case))

        case_number = 0
        is_short_circuiting = False
        is_short_circuiting_enabled = self.submission.short_circuit
        for batch_number, cases in groupby(flattened_cases, key=itemgetter(0)):
            if batch_number:
                yield IPC.BATCH_BEGIN, (batch_number,)

            for _, case in cases:
                case_number += 1

                # Stop grading if we're short circuiting
                if is_short_circuiting:
                    result = Result(case, result_flag=Result.SC)
                else:
                    result = self.grader.grade(case)

                    # If the submission was killed due to a user-initiated abort, any result is meaningless.
                    if self._abort_requested:
                        yield IPC.GRADING_ABORTED, ()
                        return

                    if result.result_flag & Result.WA:
                        # If we failed a 0-point case, we will short-circuit every case after this.
                        is_short_circuiting_enabled |= not case.points

                        # Short-circuit if we just failed a case in a batch, or if short-circuiting is currently enabled
                        # for all test cases (either this was requested by the site, or we failed a 0-point case in the
                        # past).
                        is_short_circuiting |= batch_number is not None or is_short_circuiting_enabled

                yield IPC.RESULT, (batch_number, case_number, result)

            if batch_number:
                yield IPC.BATCH_END, (batch_number,)
                is_short_circuiting &= is_short_circuiting_enabled

        yield IPC.GRADING_END, ()

    def _do_abort(self) -> None:
        self._abort_requested = True
        if self.grader:
            self.grader.abort_grading()


class ClassicJudge(Judge):
    def __init__(self, host, port, **kwargs) -> None:
        super().__init__(packet.PacketManager(host, port, self, env['id'], env['key'], **kwargs))


def sanity_check():
    if os.name == 'nt':
        print('cannot run judge on Windows', file=sys.stderr)
        return False
    else:
        # Don't allow starting up without cptbox, saves cryptic errors later on
        try:
            from .cptbox import _cptbox  # noqa: F401, we want to see if this imports
        except ImportError:
            print('cptbox must be compiled to grade!', file=sys.stderr)
            return False

        # However running as root on Linux is a Bad Idea
        if os.getuid() == 0:
            startup_warnings.append(
                'running the judge as root can be potentially unsafe, consider using an unprivileged user instead'
            )

        # Our sandbox filter is long but simple, so we can see large improvements
        # in overhead by enabling the BPF JIT for seccomp.
        bpf_jit_path = '/proc/sys/net/core/bpf_jit_enable'
        if os.path.exists(bpf_jit_path):
            with open(bpf_jit_path, 'r') as f:
                if f.read().strip() != '1':
                    startup_warnings.append(
                        'running without BPF JIT enabled, consider running '
                        '`echo 1 > /proc/sys/net/core/bpf_jit_enable` to reduce sandbox overhead'
                    )

    # _checker implements standard checker functions in C
    # we fall back to a Python implementation if it's not compiled, but it's slower
    try:
        from .checkers import _checker  # noqa: F401, we want to see if this imports
    except ImportError:
        startup_warnings.append('native checker module not found, compile _checker for optimal performance')
    return True


def make_host_port(judgeenv):
    host = judgeenv.server_host
    if ':' in host:
        host = '[%s]' % (host,)
    return '%s:%s%s' % (host, judgeenv.server_port, 's' if judgeenv.secure else '')


def main():  # pragma: no cover
    unicode_stdout_stderr()

    if not sanity_check():
        return 1

    from dmoj import judgeenv, contrib, executors

    judgeenv.load_env()

    executors.load_executors()
    contrib.load_contrib_modules()

    print('Running live judge...')

    for warning in judgeenv.startup_warnings:
        print_ansi('#ansi[Warning: %s](yellow)' % warning)
    del judgeenv.startup_warnings

    logfile = judgeenv.log_file

    try:
        logfile = logfile % env['id']
    except TypeError:
        pass

    logging.basicConfig(
        filename=logfile, level=logging.INFO, format='%(levelname)s %(asctime)s %(process)d %(module)s %(message)s'
    )

    setproctitle('DMOJ Judge %s on %s' % (env['id'], make_host_port(judgeenv)))

    judge = ClassicJudge(
        judgeenv.server_host,
        judgeenv.server_port,
        secure=judgeenv.secure,
        no_cert_check=judgeenv.no_cert_check,
        cert_store=judgeenv.cert_store,
    )
    monitor = Monitor()
    monitor.callback = judge.update_problems

    if hasattr(signal, 'SIGUSR2'):

        def update_problem_signal(signum, frame):
            judge.update_problems()

        signal.signal(signal.SIGUSR2, update_problem_signal)

    if judgeenv.api_listen:
        judge_instance = judge

        class Handler(JudgeControlRequestHandler):
            judge = judge_instance

        api_server = HTTPServer(judgeenv.api_listen, Handler)
        thread = threading.Thread(target=api_server.serve_forever)
        thread.daemon = True
        thread.start()
    else:
        api_server = None

    print()
    with monitor:
        try:
            judge.listen()
        except KeyboardInterrupt:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            judge.murder()
            if api_server:
                api_server.shutdown()


if __name__ == '__main__':
    main()
