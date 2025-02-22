import logging
import sys
import time
import traceback

import pytest
import requests

import ray
import ray._private.gcs_pubsub as gcs_pubsub
import ray.dashboard.utils as dashboard_utils
from ray._private.test_utils import format_web_url, wait_until_server_available
from ray.dashboard.modules.actor import actor_consts
from ray.dashboard.tests.conftest import *  # noqa

logger = logging.getLogger(__name__)


def test_actors(disable_aiohttp_cache, ray_start_with_dashboard):
    @ray.remote
    class Foo:
        def __init__(self, num):
            self.num = num

        def do_task(self):
            return self.num

    @ray.remote(num_gpus=1)
    class InfeasibleActor:
        pass

    foo_actors = [Foo.remote(4), Foo.remote(5)]
    infeasible_actor = InfeasibleActor.remote()  # noqa
    results = [actor.do_task.remote() for actor in foo_actors]  # noqa
    webui_url = ray_start_with_dashboard["webui_url"]
    assert wait_until_server_available(webui_url)
    webui_url = format_web_url(webui_url)

    timeout_seconds = 5
    start_time = time.time()
    last_ex = None
    while True:
        time.sleep(1)
        try:
            resp = requests.get(f"{webui_url}/logical/actors")
            resp_json = resp.json()
            resp_data = resp_json["data"]
            actors = resp_data["actors"]
            assert len(actors) == 3
            one_entry = list(actors.values())[0]
            assert "jobId" in one_entry
            assert "className" in one_entry
            assert "address" in one_entry
            assert type(one_entry["address"]) is dict
            assert "state" in one_entry
            assert "name" in one_entry
            assert "numRestarts" in one_entry
            assert "pid" in one_entry
            all_pids = {entry["pid"] for entry in actors.values()}
            assert 0 in all_pids  # The infeasible actor
            assert len(all_pids) > 1
            break
        except Exception as ex:
            last_ex = ex
        finally:
            if time.time() > start_time + timeout_seconds:
                ex_stack = (
                    traceback.format_exception(
                        type(last_ex), last_ex, last_ex.__traceback__
                    )
                    if last_ex
                    else []
                )
                ex_stack = "".join(ex_stack)
                raise Exception(f"Timed out while testing, {ex_stack}")


def test_actor_pubsub(disable_aiohttp_cache, ray_start_with_dashboard):
    timeout = 5
    assert wait_until_server_available(ray_start_with_dashboard["webui_url"]) is True
    address_info = ray_start_with_dashboard

    sub = gcs_pubsub.GcsActorSubscriber(address=address_info["gcs_address"])
    sub.subscribe()

    @ray.remote
    class DummyActor:
        def __init__(self):
            pass

    # Create a dummy actor.
    a = DummyActor.remote()

    def handle_pub_messages(msgs, timeout, expect_num):
        start_time = time.time()
        while time.time() - start_time < timeout and len(msgs) < expect_num:
            published = sub.poll(timeout=timeout)
            for _, actor_data in published:
                if actor_data is None:
                    continue
                msgs.append(actor_data)

    msgs = []
    handle_pub_messages(msgs, timeout, 3)
    # Assert we received published actor messages with state
    # DEPENDENCIES_UNREADY, PENDING_CREATION and ALIVE.
    assert len(msgs) == 3, msgs

    # Kill actor.
    ray.kill(a)
    handle_pub_messages(msgs, timeout, 4)

    # Assert we received published actor messages with state DEAD.
    assert len(msgs) == 4

    def actor_table_data_to_dict(message):
        return dashboard_utils.message_to_dict(
            message,
            {
                "actorId",
                "parentId",
                "jobId",
                "workerId",
                "rayletId",
                "callerId",
                "taskId",
                "parentTaskId",
                "sourceActorId",
                "placementGroupId",
            },
            including_default_value_fields=False,
        )

    non_state_keys = ("actorId", "jobId")

    for msg in msgs:
        actor_data_dict = actor_table_data_to_dict(msg)
        # DEPENDENCIES_UNREADY is 0, which would not be kept in dict. We
        # need check its original value.
        if msg.state == 0:
            assert len(actor_data_dict) > 5
            for k in non_state_keys:
                assert k in actor_data_dict
        # For status that is not DEPENDENCIES_UNREADY, only states fields will
        # be published.
        elif actor_data_dict["state"] in ("ALIVE", "DEAD"):
            assert actor_data_dict.keys() >= {
                "state",
                "address",
                "timestamp",
                "pid",
                "rayNamespace",
            }
        elif actor_data_dict["state"] == "PENDING_CREATION":
            assert actor_data_dict.keys() == {
                "state",
                "address",
                "actorId",
                "jobId",
                "ownerAddress",
                "className",
                "serializedRuntimeEnv",
                "rayNamespace",
                "functionDescriptor",
                "actorCreationDummyObjectId",
            }
        else:
            raise Exception("Unknown state: {}".format(actor_data_dict["state"]))


def test_nil_node(enable_test_module, disable_aiohttp_cache, ray_start_with_dashboard):
    assert wait_until_server_available(ray_start_with_dashboard["webui_url"]) is True
    webui_url = ray_start_with_dashboard["webui_url"]
    assert wait_until_server_available(webui_url)
    webui_url = format_web_url(webui_url)

    @ray.remote(num_gpus=1)
    class InfeasibleActor:
        pass

    infeasible_actor = InfeasibleActor.remote()  # noqa

    timeout_seconds = 5
    start_time = time.time()
    last_ex = None
    while True:
        time.sleep(1)
        try:
            resp = requests.get(f"{webui_url}/logical/actors")
            resp_json = resp.json()
            resp_data = resp_json["data"]
            actors = resp_data["actors"]
            assert len(actors) == 1
            response = requests.get(webui_url + "/test/dump?key=node_actors")
            response.raise_for_status()
            result = response.json()
            assert actor_consts.NIL_NODE_ID not in result["data"]["nodeActors"]
            break
        except Exception as ex:
            last_ex = ex
        finally:
            if time.time() > start_time + timeout_seconds:
                ex_stack = (
                    traceback.format_exception(
                        type(last_ex), last_ex, last_ex.__traceback__
                    )
                    if last_ex
                    else []
                )
                ex_stack = "".join(ex_stack)
                raise Exception(f"Timed out while testing, {ex_stack}")


def test_actor_cleanup(
    disable_aiohttp_cache, reduce_actor_cache, ray_start_with_dashboard
):
    @ray.remote
    class Foo:
        def __init__(self, num):
            self.num = num

        def do_task(self):
            return self.num

    @ray.remote(num_gpus=1)
    class InfeasibleActor:
        pass

    infeasible_actor = InfeasibleActor.remote()  # noqa

    foo_actors = [
        Foo.remote(1),
        Foo.remote(2),
        Foo.remote(3),
        Foo.remote(4),
        Foo.remote(5),
        Foo.remote(6),
    ]
    results = [actor.do_task.remote() for actor in foo_actors]  # noqa
    webui_url = ray_start_with_dashboard["webui_url"]
    assert wait_until_server_available(webui_url)
    webui_url = format_web_url(webui_url)

    timeout_seconds = 8
    start_time = time.time()
    last_ex = None
    while True:
        time.sleep(1)
        try:
            resp = requests.get(f"{webui_url}/logical/actors")
            resp_json = resp.json()
            resp_data = resp_json["data"]
            actors = resp_data["actors"]
            # Although max cache is 3, there should be 7 actors
            # because they are all still alive.
            assert len(actors) == 7

            break
        except Exception as ex:
            last_ex = ex
        finally:
            if time.time() > start_time + timeout_seconds:
                ex_stack = (
                    traceback.format_exception(
                        type(last_ex), last_ex, last_ex.__traceback__
                    )
                    if last_ex
                    else []
                )
                ex_stack = "".join(ex_stack)
                raise Exception(f"Timed out while testing, {ex_stack}")

    # kill
    ray.kill(infeasible_actor)
    [ray.kill(foo_actor) for foo_actor in foo_actors]
    # Wait 5 seconds for cleanup to finish
    time.sleep(5)

    # Check only three remaining in cache
    start_time = time.time()
    while True:
        time.sleep(1)
        try:
            resp = requests.get(f"{webui_url}/logical/actors")
            resp_json = resp.json()
            resp_data = resp_json["data"]
            actors = resp_data["actors"]
            # Max cache is 3 so only 3 actors should be left.
            assert len(actors) == 3

            break
        except Exception as ex:
            last_ex = ex
        finally:
            if time.time() > start_time + timeout_seconds:
                ex_stack = (
                    traceback.format_exception(
                        type(last_ex), last_ex, last_ex.__traceback__
                    )
                    if last_ex
                    else []
                )
                ex_stack = "".join(ex_stack)
                raise Exception(f"Timed out while testing, {ex_stack}")


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
