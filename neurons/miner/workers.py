import asyncio
import base64
import time
import typing
import urllib.parse

import aiohttp
import bittensor as bt
from aiohttp import ClientTimeout
from aiohttp.helpers import sentinel
from common.protocol import PullTask, SubmitResults
from common.version import NEURONS_VERSION, compare_versions

from miner import ValidatorSelector


NETWORK_DELAY_TIME_BUFFER = 60


async def worker_routine(
    endpoint: str, wallet: bt.wallet, metagraph: bt.metagraph, validator_selector: ValidatorSelector
) -> None:
    bt.logging.info(f"Worker ({endpoint}) started")
    generate_url = urllib.parse.urljoin(endpoint, "/generate/")

    while True:
        await _complete_one_task(generate_url, wallet, metagraph, validator_selector)


async def _complete_one_task(
    generate_url: str, wallet: bt.wallet, metagraph: bt.metagraph, validator_selector: ValidatorSelector
) -> None:
    validator_uid = validator_selector.get_next_validator_to_query()
    if validator_uid is None:
        await asyncio.sleep(10.0)
        return

    dendrite = bt.dendrite(wallet=wallet)

    # Setting cooldown to prevent selecting the same validator for concurrent task.
    validator_selector.set_cooldown(validator_uid, int(time.time()) + 60)

    pull = await _pull_task(dendrite, metagraph, validator_uid)
    if pull.task is None:
        bt.logging.warning(
            f"Validator [{metagraph.hotkeys[validator_uid]}] is not serving tasks or miner is"
            f" violating rules. Check the trace logs for more details."
        )
        return

    bt.logging.debug(f"Task received. Prompt: {pull.task.prompt}. Version: {pull.version}")

    # Updating cooldown. Validator won't give new tasks until this one is submitted.
    validator_selector.set_cooldown(validator_uid, pull.submit_before)

    timeout = pull.submit_before - time.time() - NETWORK_DELAY_TIME_BUFFER if pull.submit_before > 0 else None
    results = await _generate(generate_url, pull.task.prompt, timeout=timeout)
    if results is None:
        return

    submit = None
    for _ in range(3):
        submit = await _submit_results(wallet, dendrite, metagraph, validator_uid, pull, results)
        if submit.feedback is not None:
            break

        submit = None
        bt.logging.debug(
            f"Failed to submit results to [{metagraph.hotkeys[validator_uid]}]. "
            f"Making another attempt in 10 seconds"
        )
        await asyncio.sleep(10.0)

    if submit is None:
        bt.logging.warning(
            f"Validator [{metagraph.hotkeys[validator_uid]}] failed to receive results or miner is"
            f" violating rules. Check the trace logs for more details."
        )
        return

    _log_feedback(submit)

    validator_selector.set_cooldown(validator_uid, submit.cooldown_until)


async def _pull_task(dendrite: bt.dendrite, metagraph: bt.metagraph, validator_uid: int) -> PullTask:
    synapse = PullTask()
    response = typing.cast(
        PullTask,
        await dendrite.call(
            target_axon=metagraph.axons[validator_uid], synapse=synapse, deserialize=False, timeout=12.0
        ),
    )
    if response.version is not None:
        compare_versions(
            miner=NEURONS_VERSION, validator=response.version, validator_hotkey=metagraph.hotkeys[validator_uid]
        )
    return response


async def _submit_results(
    wallet: bt.wallet, dendrite: bt.dendrite, metagraph: bt.metagraph, validator_uid: int, pull: PullTask, results: str
) -> SubmitResults:
    nonce = time.time_ns()
    prompt = pull.task.prompt if pull.task is not None else None
    signature = dendrite.keypair.sign(f"{nonce}{prompt}{metagraph.hotkeys[validator_uid]}{wallet.hotkey.ss58_address}")
    synapse = SubmitResults(task=pull.task, results=results, nonce=nonce, signature=base64.b64encode(signature))
    response = typing.cast(
        SubmitResults,
        await dendrite.call(
            target_axon=metagraph.axons[validator_uid],
            synapse=synapse,
            deserialize=False,
            timeout=300.0,
        ),
    )
    return response


def _log_feedback(submit: SubmitResults) -> None:
    feedback = submit.feedback
    if submit.task is None or feedback is None:
        return
    bt.logging.debug(f"Feedback received. Prompt: {submit.task.prompt}. Score: {feedback.task_fidelity_score}")
    bt.logging.debug(
        f"Average score: {feedback.average_fidelity_score}. "
        f"Accepted results (last 8h): {feedback.generations_within_8_hours}. "
        f"Reward: {feedback.current_miner_reward}."
    )


async def _generate(generate_url: str, prompt: str, timeout: float | None = None) -> str | None:
    bt.logging.debug(f"Generating for prompt: {prompt} with timeout {timeout} seconds")

    client_timeout = ClientTimeout(total=timeout) if timeout is not None else sentinel
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        try:
            async with session.post(generate_url, data={"prompt": prompt}) as response:
                if response.status == 200:
                    results = await response.text()
                    bt.logging.debug(f"Generation completed. Size: {len(results)}")
                    return results
                else:
                    bt.logging.error(f"Generation failed with code: {response.status}")
        except aiohttp.ClientConnectorError:
            bt.logging.error(f"Failed to connect to the endpoint. The endpoint might be inaccessible: {generate_url}.")
        except TimeoutError:
            bt.logging.error(f"The request to the endpoint timed out: {generate_url}")
        except aiohttp.ClientError as e:
            bt.logging.error(f"An unexpected client error occurred: {e} ({generate_url})")
        except Exception as e:
            bt.logging.error(f"An unexpected error occurred: {e} ({generate_url})")
