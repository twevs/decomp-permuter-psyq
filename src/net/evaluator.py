"""This file runs as a free-standing program within a sandbox, and processes
permutation requests. It communicates with the outside world on stdin/stdout."""
import base64
from dataclasses import dataclass
import math
from multiprocessing import Process, Queue
import os
import queue
import struct
import sys
from tempfile import mkstemp
import threading
import time
import traceback
from typing import BinaryIO, Counter, Dict, List, Optional, Set, Tuple, Union
import zlib

from nacl.secret import SecretBox

from ..candidate import CandidateResult
from ..compiler import Compiler
from ..error import CandidateConstructionFailure
from ..helpers import exception_to_string, static_assert_unreachable
from ..permuter import EvalError, EvalResult, Permuter
from ..profiler import Profiler
from ..scorer import Scorer
from .core import (
    FilePort,
    PermuterData,
    Port,
    json_prop,
    permuter_data_from_json,
)


def _fix_stdout() -> None:
    """Redirect stdout to stderr to make print() debugging work. This function
    *must* be called at startup for each (sub)process, since we use stdout for
    our own communication purposes."""
    sys.stdout = sys.stderr

    # In addition, we set stderr to flush on newlines, which not happen by
    # default when it is piped. (Requires Python 3.7, but we can assume that's
    # available inside the sandbox.)
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore


def _setup_port(secret: bytes) -> Port:
    """Set up communication with the outside world."""
    port = FilePort(
        sys.stdin.buffer,
        sys.stdout.buffer,
        SecretBox(secret),
        "server",
        is_client=False,
    )

    # Follow the controlling process's sanity check protocol.
    magic = port.receive()
    port.send(magic)

    return port


def _create_permuter(data: PermuterData) -> Permuter:
    fd, path = mkstemp(suffix=".o", prefix="permuter", text=False)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.target_o_bin)
        scorer = Scorer(target_o=path, stack_differences=data.stack_differences)
    finally:
        os.unlink(path)

    fd, path = mkstemp(suffix=".sh", prefix="permuter", text=True)
    try:
        os.chmod(fd, 0o755)
        with os.fdopen(fd, "w") as f2:
            f2.write(data.compile_script)
        compiler = Compiler(compile_cmd=path, show_errors=False)

        return Permuter(
            dir="unused",
            fn_name=data.fn_name,
            compiler=compiler,
            scorer=scorer,
            source_file=data.filename,
            source=data.source,
            force_seed=None,
            force_rng_seed=None,
            keep_prob=data.keep_prob,
            need_profiler=data.need_profiler,
            need_all_sources=False,
            show_errors=False,
            better_only=False,
            best_only=False,
        )
    except:
        os.unlink(path)
        raise


def _remove_permuter(perm: Permuter) -> None:
    os.unlink(perm.compiler.compile_cmd)


def _send_result(perm_id: str, time_us: float, res: EvalResult, port: Port) -> None:
    if isinstance(res, EvalError):
        port.send_json(
            {
                "type": "result",
                "id": perm_id,
                "time_us": time_us,
                "error": res.exc_str,
            }
        )
        return

    compressed_source = getattr(res, "compressed_source")

    obj = {
        "type": "result",
        "id": perm_id,
        "time_us": time_us,
        "score": res.score,
        "has_source": compressed_source is not None,
    }
    if res.hash is not None:
        obj["hash"] = res.hash
    if res.profiler is not None:
        obj["profiler"] = {
            st.name: res.profiler.time_stats[st] for st in Profiler.StatType
        }

    port.send_json(obj)

    if compressed_source is not None:
        port.send(compressed_source)


@dataclass
class AddPermuter:
    perm_id: str
    data: PermuterData


@dataclass
class AddPermuterLocal:
    perm_id: str
    permuter: Permuter


@dataclass
class RemovePermuter:
    perm_id: str


@dataclass
class WorkDone:
    perm_id: str
    time_us: float
    result: EvalResult


@dataclass
class Work:
    perm_id: str
    seed: int


class NeedMoreWork:
    pass


LocalWork = Tuple[Union[AddPermuterLocal, RemovePermuter], int]
GlobalWork = Tuple[Work, int]
Task = Union[AddPermuter, RemovePermuter, Work, WorkDone, NeedMoreWork]


def multiprocess_worker(
    worker_queue: "Queue[GlobalWork]",
    local_queue: "Queue[LocalWork]",
    task_queue: "Queue[Task]",
) -> None:
    _fix_stdout()

    permuters: Dict[str, Permuter] = {}
    timestamp = 0

    while True:
        try:
            work, required_timestamp = worker_queue.get(block=False)
        except queue.Empty:
            task_queue.put(NeedMoreWork())
            work, required_timestamp = worker_queue.get()
        while True:
            try:
                block = timestamp < required_timestamp
                task, timestamp = local_queue.get(block=block)
            except queue.Empty:
                break
            if isinstance(task, AddPermuterLocal):
                permuters[task.perm_id] = task.permuter
            elif isinstance(task, RemovePermuter):
                del permuters[task.perm_id]
            else:
                static_assert_unreachable(task)

        time_before = time.time()

        permuter = permuters[work.perm_id]
        result = permuter.try_eval_candidate(work.seed)
        if isinstance(result, CandidateResult) and permuter.should_output(result):
            permuter.record_result(result)

        # Compress the source within the worker. (Why waste a free
        # multi-threading opportunity?)
        if isinstance(result, CandidateResult):
            compressed_source: Optional[bytes] = None
            if result.source is not None:
                compressed_source = zlib.compress(result.source.encode("utf-8"))
            setattr(result, "compressed_source", compressed_source)
            result.source = None

        time_us = int((time.time() - time_before) * 10 ** 6)
        task_queue.put(WorkDone(perm_id=work.perm_id, time_us=time_us, result=result))


def read_loop(task_queue: "Queue[Task]", port: Port) -> None:
    try:
        while True:
            item = port.receive_json()
            msg_type = json_prop(item, "type", str)
            if msg_type == "add":
                perm_id = json_prop(item, "id", str)
                source = port.receive().decode("utf-8")
                target_o_bin = port.receive()
                data = permuter_data_from_json(item, source, target_o_bin)
                task_queue.put(AddPermuter(perm_id=perm_id, data=data))

            elif msg_type == "remove":
                perm_id = json_prop(item, "id", str)
                task_queue.put(RemovePermuter(perm_id=perm_id))

            elif msg_type == "work":
                perm_id = json_prop(item, "id", str)
                seed = json_prop(item, "seed", int)
                task_queue.put(Work(perm_id=perm_id, seed=seed))

            else:
                raise Exception(f"Invalid message type {msg_type}")

    except EOFError:
        # Port closed from the other side. Silently exit, to avoid ugly error
        # messages and to ensure that the Docker container really stops and
        # gets removed. (The parent server has a "finally:" that does that, but
        # it's evidently not 100% trustworthy. I'm speculating that pystray
        # might be to blame, by reverting the signal handler for SIGINT to
        # the default, making Ctrl+C kill the program directly without firing
        # "finally"s. Either way, defense in depth here doesn't hurt, since
        # leaking Docker containers is pretty bad.)
        os._exit(1)

    except Exception:
        traceback.print_exc()
        os._exit(1)


def main() -> None:
    secret = base64.b64decode(os.environ["SECRET"])
    del os.environ["SECRET"]
    os.environ["PERMUTER_IS_REMOTE"] = "1"

    port = _setup_port(secret)
    _fix_stdout()

    obj = port.receive_json()
    num_cores = json_prop(obj, "num_cores", float)
    num_threads = math.ceil(num_cores)

    worker_queue: "Queue[GlobalWork]" = Queue()
    task_queue: "Queue[Task]" = Queue()
    local_queues: "List[Queue[LocalWork]]" = []

    for i in range(num_threads):
        local_queue: "Queue[LocalWork]" = Queue()
        p = Process(
            target=multiprocess_worker,
            args=(worker_queue, local_queue, task_queue),
        )
        p.start()
        local_queues.append(local_queue)

    reader_thread = threading.Thread(target=read_loop, args=(task_queue, port))
    reader_thread.start()

    remaining_work: Counter[str] = Counter()
    should_remove: Set[str] = set()
    permuters: Dict[str, Permuter] = {}
    timestamp = 0

    def try_remove(perm_id: str) -> None:
        nonlocal timestamp
        assert perm_id in permuters
        if perm_id not in should_remove or remaining_work[perm_id] != 0:
            return
        del remaining_work[perm_id]
        should_remove.remove(perm_id)
        timestamp += 1
        for queue in local_queues:
            queue.put((RemovePermuter(perm_id=perm_id), timestamp))
        _remove_permuter(permuters[perm_id])
        del permuters[perm_id]

    while True:
        item = task_queue.get()

        if isinstance(item, AddPermuter):
            assert item.perm_id not in permuters

            msg: Dict[str, object] = {
                "type": "init",
                "id": item.perm_id,
            }

            time_before = time.time()
            try:
                # Construct a permuter. This involves a compilation on the main
                # thread, which isn't great but we can live with it for now.
                permuter = _create_permuter(item.data)

                if permuter.base_score != item.data.base_score:
                    _remove_permuter(permuter)
                    score_str = f"{permuter.base_score} vs {item.data.base_score}"
                    if permuter.base_hash == item.data.base_hash:
                        hash_str = "same hash; different Python or permuter versions?"
                    else:
                        hash_str = "different hash; different objdump versions?"
                    raise CandidateConstructionFailure(
                        f"mismatching score: {score_str} ({hash_str})"
                    )

                permuters[item.perm_id] = permuter

                msg["success"] = True
                msg["base_score"] = permuter.base_score
                msg["base_hash"] = permuter.base_hash

                # Tell all the workers about the new permuter.
                # TODO: ideally we would also seed their Candidate lru_cache's
                # to avoid all workers having to parse the source...
                timestamp += 1
                for queue in local_queues:
                    queue.put(
                        (
                            AddPermuterLocal(perm_id=item.perm_id, permuter=permuter),
                            timestamp,
                        )
                    )
            except Exception as e:
                # This shouldn't practically happen, since the client compiled
                # the code successfully. Print a message if it does.
                msg["success"] = False
                msg["error"] = exception_to_string(e)
                if isinstance(e, CandidateConstructionFailure):
                    print(e.message)
                else:
                    traceback.print_exc()

            msg["time_us"] = int((time.time() - time_before) * 10 ** 6)
            port.send_json(msg)

        elif isinstance(item, RemovePermuter):
            # Silently ignore requests to remove permuters that have already
            # been removed, which can occur when AddPermuter fails.
            if item.perm_id in permuters:
                should_remove.add(item.perm_id)
                try_remove(item.perm_id)

        elif isinstance(item, WorkDone):
            remaining_work[item.perm_id] -= 1
            try_remove(item.perm_id)
            _send_result(item.perm_id, item.time_us, item.result, port)

        elif isinstance(item, Work):
            remaining_work[item.perm_id] += 1
            worker_queue.put((item, timestamp))

        elif isinstance(item, NeedMoreWork):
            port.send_json({"type": "need_work"})

        else:
            static_assert_unreachable(item)


if __name__ == "__main__":
    main()