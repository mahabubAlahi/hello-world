# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2021 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module contains the behaviours for the 'abci' skill."""
import binascii
import datetime
import pprint
from functools import partial
from typing import Callable, Generator, List, Type, cast

from aea.exceptions import enforce
from aea.skills.behaviours import FSMBehaviour
from web3.types import Wei

from packages.fetchai.protocols.signing import SigningMessage
from packages.valory.skills.abstract_round_abci.behaviour_utils import (
    BaseState,
    DONE_EVENT,
)
from packages.valory.skills.price_estimation_abci.helpers.gnosis_safe import (
    get_deploy_safe_tx,
)
from packages.valory.skills.price_estimation_abci.models.payloads import (
    DeploySafePayload,
    EstimatePayload,
    FinalizationTxPayload,
    ObservationPayload,
    RegistrationPayload,
    SignaturePayload,
    TransactionHashPayload,
)
from packages.valory.skills.price_estimation_abci.models.rounds import (
    CollectObservationRound,
    CollectSignatureRound,
    ConsensusReachedRound,
    DeploySafeRound,
    EstimateConsensusRound,
    FinalizationRound,
    RegistrationRound,
    TxHashRound,
)


SIGNATURE_LENGTH = 65


class PriceEstimationConsensusBehaviour(FSMBehaviour):
    """This behaviour manages the consensus stages for the price estimation."""

    def setup(self) -> None:
        """Set up the behaviour."""
        self._register_states(
            [
                InitialDelayState,  # type: ignore
                RegistrationBehaviour,  # type: ignore
                DeploySafeBehaviour,  # type: ignore
                ObserveBehaviour,  # type: ignore
                EstimateBehaviour,  # type: ignore
                TransactionHashBehaviour,  # type: ignore
                SignatureBehaviour,  # type: ignore
                FinalizeBehaviour,  # type: ignore
                EndBehaviour,  # type: ignore
            ]
        )

    def teardown(self) -> None:
        """Tear down the behaviour"""

    def _register_states(self, state_classes: List[Type[BaseState]]) -> None:
        """Register a list of states."""
        enforce(
            len(state_classes) != 0,
            "empty list of state classes",
            exception_class=ValueError,
        )
        self._register_state(state_classes[0], initial=True)
        for state_cls in state_classes[1:]:
            self._register_state(state_cls)

        for index in range(len(state_classes) - 1):
            before, after = state_classes[index], state_classes[index + 1]
            self.register_transition(before.state_id, after.state_id, DONE_EVENT)

    def _register_state(
        self, state_cls: Type[BaseState], initial: bool = False
    ) -> None:
        """Register state."""
        name = state_cls.state_id
        return super().register_state(
            state_cls.state_id,
            state_cls(name=name, skill_context=self.context),
            initial=initial,
        )

    def get_wait_tendermint_rpc_is_ready(self) -> Callable:
        """
        Wait Tendermint RPC server is up.

        This method will return a function that returns
        False until 'initial_delay' seconds (a skill parameter)
        have elapsed since the call of the method.

        :return: the function used to wait.
        """

        def _check_time(expected_time: datetime.datetime) -> bool:
            return datetime.datetime.now() > expected_time

        initial_delay = self.context.params.initial_delay
        date = datetime.datetime.now() + datetime.timedelta(0, initial_delay)
        return partial(_check_time, date)

    def wait_observation_round(self) -> bool:
        """Wait registration threshold is reached."""
        return (
            self.context.state.period.current_round_id
            == CollectObservationRound.round_id
        )

    def wait_estimate_round(self) -> bool:
        """Wait observation threshold is reached."""
        return (
            self.context.state.period.current_round_id
            == EstimateConsensusRound.round_id
        )

    def wait_consensus_round(self) -> bool:
        """Wait estimate threshold is reached."""
        return (
            self.context.state.period.current_round_id == ConsensusReachedRound.round_id
        )


class InitialDelayState(BaseState):  # pylint: disable=too-many-ancestors
    """Wait for some seconds until Tendermint nodes are running."""

    state_id = "initial_delay"

    def async_act(self) -> None:  # type: ignore
        """Do the action."""
        delay = self.context.params.initial_delay
        yield from self.sleep(delay)
        self.set_done()


class RegistrationBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Register to the next round."""

    state_id = "register"

    def async_act(self) -> None:  # type: ignore
        """
        Do the action.

        Steps:
        - Build a registration transaction
        - Send the transaction and wait for it to be mined
        - Wait until ABCI application transitions to the next round.
        - Go to the next behaviour state.
        """
        self._log_start()
        payload = RegistrationPayload(self.context.agent_address)
        stop_condition = self.is_round_ended(RegistrationRound.round_id)
        yield from self._send_transaction(payload, stop_condition=stop_condition)
        yield from self.wait_until_round_end(RegistrationRound.round_id)
        self._log_end()
        self.set_done()


class DeploySafeBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Deploy Safe."""

    state_id = "deploy_safe"

    def async_act(self) -> Generator:
        """
        Do the action.

        Steps:
        - TODO
        """
        self._log_start()
        if self.context.agent_address != self.period_state.safe_sender_address:
            self.not_deployer_act()
        else:
            yield from self.deployer_act()
        yield from self.wait_until_round_end(DeploySafeRound.round_id)
        self.context.logger.info(
            f"Safe contract address: {self.period_state.safe_contract_address}"
        )
        self._log_end()
        self.set_done()

    def not_deployer_act(self) -> None:
        """Do the non-deployer action."""
        self.context.logger.info(
            "I am not the designated sender, waiting until next round..."
        )

    def deployer_act(self) -> Generator:
        """Do the deployer action."""
        self.context.logger.info(
            "I am the designated sender, deploying the safe contract..."
        )
        contract_address = yield from self._send_deploy_transaction()
        payload = DeploySafePayload(self.context.agent_address, contract_address)
        stop_condition = self.is_round_ended(DeploySafeRound.round_id)
        yield from self._send_transaction(payload, stop_condition=stop_condition)

    def _send_deploy_transaction(self) -> Generator[None, None, str]:
        ethereum_node_url = self.context.params.ethereum_node_url
        owners = self.period_state.participants
        threshold = self.context.params.consensus_params.two_thirds_threshold
        tx_params, contract_address = get_deploy_safe_tx(
            ethereum_node_url, self.context.agent_address, list(owners), threshold
        )
        tx_hash = yield from self._send_raw_transaction(dict(tx_params))
        self.context.logger.info(f"Deployment tx hash: {tx_hash}")
        return contract_address


class ObserveBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Observe price estimate."""

    state_id = "observe"

    def async_act(self) -> Generator:
        """
        Do the action.

        Steps:
        - Ask the configured API the price of a currency
        - Build an observation transaction
        - Wait until ABCI application transitions to the next round.
        - Go to the next behaviour state.
        """
        self._log_start()
        currency_id = self.context.params.currency_id
        convert_id = self.context.params.convert_id
        observation = self.context.price_api.get_price(currency_id, convert_id)
        self.context.logger.info(
            f"Got observation of {currency_id} price in {convert_id} from {self.context.price_api.api_id}: {observation}"
        )
        payload = ObservationPayload(self.context.agent_address, observation)
        stop_condition = self.is_round_ended(CollectObservationRound.round_id)
        yield from self._send_transaction(payload, stop_condition=stop_condition)
        yield from self.wait_until_round_end(CollectObservationRound.round_id)
        self._log_end()
        self.set_done()


class EstimateBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Estimate price."""

    state_id = "estimate"

    def async_act(self) -> Generator:
        """
        Do the action.

        Steps:
        - Run the script to compute the estimate starting from the shared observations
        - Build an estimate transaction
        - Send the transaction and wait for it to be mined
        - Wait until ABCI application transitions to the next round.
        - Go to the next behaviour state.
        """
        self._log_start()
        currency_id = self.context.params.currency_id
        convert_id = self.context.params.convert_id
        observation_payloads = self.period_state.observations
        observations = [obs_payload.observation for obs_payload in observation_payloads]
        self.context.logger.info(
            f"Using observations {observations} to compute the estimate."
        )
        estimate = self.context.estimator.aggregate(observations)
        self.context.logger.info(
            f"Got estimate of {currency_id} price in {convert_id}: {estimate}"
        )
        payload = EstimatePayload(self.context.agent_address, estimate)
        stop_condition = self.is_round_ended(EstimateConsensusRound.round_id)
        yield from self._send_transaction(payload, stop_condition=stop_condition)
        yield from self.wait_until_round_end(EstimateConsensusRound.round_id)
        self._log_end()
        self.set_done()


class TransactionHashBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Share the transaction hash for the signature round."""

    state_id = "tx_hash"

    def async_act(self) -> None:  # type: ignore
        """
        Do the action.

        Steps:
        - TODO
        """
        self._log_start()
        if self.context.agent_address != self.period_state.safe_sender_address:
            self.not_sender_act()
        else:
            yield from self.sender_act()
        yield from self.wait_until_round_end(TxHashRound.round_id)
        self._log_end()
        self.set_done()

    def not_sender_act(self) -> None:
        """Do the non-deployer action."""
        self.context.logger.info(
            "I am not the designated sender, waiting until next round..."
        )

    def sender_act(self) -> Generator[None, None, None]:
        """Do the deployer action."""
        self.context.logger.info(
            "I am the designated sender, committing the transaction hash..."
        )
        self.context.logger.info(
            f"Consensus reached on estimate: {self.period_state.most_voted_estimate}"
        )
        data = self.period_state.encoded_estimate
        safe_tx = self._get_safe_tx(self.context.agent_address, data)
        safe_tx_hash = safe_tx.safe_tx_hash.hex()[2:]
        self.context.logger.info(f"Hash of the Safe transaction: {safe_tx_hash}")
        payload = TransactionHashPayload(self.context.agent_address, safe_tx_hash)
        stop_condition = self.is_round_ended(TxHashRound.round_id)
        yield from self._send_transaction(payload, stop_condition=stop_condition)


class SignatureBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Signature state."""

    state_id = "sign"

    def async_act(self) -> Generator:
        """Do the act."""
        self._log_start()
        signature_hex = yield from self._get_safe_tx_signature()
        payload = SignaturePayload(self.context.agent_address, signature_hex)
        stop_condition = self.is_round_ended(CollectSignatureRound.round_id)
        yield from self._send_transaction(payload, stop_condition=stop_condition)
        yield from self.wait_until_round_end(CollectSignatureRound.round_id)
        self._log_end()
        self.set_done()

    def _get_safe_tx_signature(self) -> Generator[None, None, str]:
        # is_deprecated_mode=True because we want to call Account.signHash,
        # which is the same used by gnosis-py
        safe_tx_hash_bytes = binascii.unhexlify(self.period_state.safe_tx_hash)
        self._send_signing_request(safe_tx_hash_bytes, is_deprecated_mode=True)
        signature_response = yield from self.wait_for_message()
        signature_hex = cast(SigningMessage, signature_response).signed_message.body
        # remove the leading '0x'
        signature_hex = signature_hex[2:]
        self.context.logger.info(f"Signature: {signature_hex}")
        return signature_hex


class FinalizeBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Finalize state."""

    state_id = "finalize"

    def async_act(self) -> Generator[None, None, None]:
        """Do the act."""
        self._log_start()
        if self.context.agent_address != self.period_state.safe_sender_address:
            self.not_sender_act()
        else:
            yield from self.sender_act()
        yield from self.wait_until_round_end(FinalizationRound.round_id)
        self._log_end()
        self.set_done()

    def not_sender_act(self) -> None:
        """Do the non-sender action."""
        self.context.logger.info(
            "I am not the designated sender, waiting until next round..."
        )

    def sender_act(self) -> Generator[None, None, None]:
        """Do the sender action."""
        self.context.logger.info(
            "I am the designated sender, sending the safe transaction..."
        )
        tx_hash = yield from self._send_safe_transaction()
        self.context.logger.info(
            f"Transaction hash of the final transaction: {tx_hash}"
        )
        self.context.logger.info(
            f"Signatures: {pprint.pformat(self.context.state.period_state.participant_to_signature)}"
        )
        payload = FinalizationTxPayload(self.context.agent_address, tx_hash)
        stop_condition = self.is_round_ended(FinalizationRound.round_id)
        yield from self._send_transaction(payload, stop_condition=stop_condition)

    def _send_safe_transaction(self) -> Generator[None, None, str]:
        """Send a Safe transaction using the participants' signatures."""
        data = self.period_state.encoded_estimate

        # compose final signature (need to be sorted!)
        final_signature = b""
        for signer in self.period_state.sorted_addresses:
            if signer not in self.period_state.participant_to_signature:
                continue
            signature = self.period_state.participant_to_signature[signer]
            signature_bytes = binascii.unhexlify(signature)
            final_signature += signature_bytes

        safe_tx = self._get_safe_tx(self.context.agent_address, data)
        safe_tx.signatures = final_signature
        safe_tx.call(self.context.agent_address)

        tx_gas_price = safe_tx.gas_price or safe_tx.w3.eth.gas_price
        tx_parameters = {
            "from": self.context.agent_address,
            "gasPrice": tx_gas_price,
        }
        transaction_dict = safe_tx.w3_tx.buildTransaction(tx_parameters)
        transaction_dict["gas"] = Wei(
            max(transaction_dict["gas"] + 75000, safe_tx.recommended_gas())
        )
        transaction_dict["nonce"] = safe_tx.w3.eth.get_transaction_count(
            safe_tx.w3.toChecksumAddress(self.context.agent_address)
        )
        tx_hash = yield from self._send_raw_transaction(transaction_dict)
        self.context.logger.info(f"Finalization tx hash: {tx_hash}")
        return tx_hash


class EndBehaviour(BaseState):  # pylint: disable=too-many-ancestors
    """Final state."""

    state_id = "end"

    def async_act(self) -> Generator:
        """Do the act."""
        self.context.logger.info(
            f"Finalized estimate: {self.period_state.most_voted_estimate} with transaction hash: {self.period_state.final_tx_hash}"
        )
        self.context.logger.info("Period end.")
        self.set_done()
        # dummy 'yield' to return a generator
        yield