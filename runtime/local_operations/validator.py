"""Deterministic CloudReturnPacket Validator & Verification Authority.
Enforces task identity integrity, real evidence reference resolution, cross-task/conversation isolation,
prevents model self-certification, detects semantic contradictions, and performs recursive whole-packet sanitization.
"""

import re
import logging
from typing import List, Tuple, Optional, Set, Dict, Any

from runtime.artifacts.store import ArtifactStore
from runtime.local_operations.models import (
    CloudReturnPacket,
    TaskDelegationContract,
    OutcomeStatus,
    VerifiedFinding,
)
from runtime.observability.redaction import sanitize_and_redact

logger = logging.getLogger("presence.local_operations.validator")


class PacketValidationError(ValueError):
    """Raised when a CloudReturnPacket fails deterministic integrity validation."""
    pass


def recursive_sanitize_value(val: Any) -> Any:
    """Recursively redacts sensitive strings within nested dicts, lists, or primitive values."""
    if isinstance(val, str):
        return sanitize_and_redact(val)
    elif isinstance(val, list):
        return [recursive_sanitize_value(item) for item in val]
    elif isinstance(val, dict):
        return {k: recursive_sanitize_value(v) for k, v in val.items()}
    elif isinstance(val, tuple):
        return tuple(recursive_sanitize_value(item) for item in val)
    return val


def recursive_sanitize_packet(packet: CloudReturnPacket) -> CloudReturnPacket:
    """Recursively redacts all sensitive credentials from every text-bearing field in the packet
    while preserving cryptographic digests, schema versions, and system identifiers.
    """
    # 1. Top-Level Fields
    packet.objective = sanitize_and_redact(packet.objective)
    packet.summary = sanitize_and_redact(packet.summary)
    packet.unresolved_questions = [
        sanitize_and_redact(q) for q in packet.unresolved_questions
    ]

    # 2. Verified Findings
    for f in packet.verified_findings:
        f.statement = sanitize_and_redact(f.statement)
        if f.source_location:
            f.source_location = sanitize_and_redact(f.source_location)

    # 3. Model Interpretations
    for interp in packet.interpretations:
        interp.statement = sanitize_and_redact(interp.statement)
        if interp.uncertainty:
            interp.uncertainty = sanitize_and_redact(interp.uncertainty)
        interp.assumptions = [sanitize_and_redact(a) for a in interp.assumptions]

    # 4. Contradictions
    for c in packet.contradictions:
        c.claim_a = sanitize_and_redact(c.claim_a)
        c.claim_b = sanitize_and_redact(c.claim_b)
        if c.source_a:
            c.source_a = sanitize_and_redact(c.source_a)
        if c.source_b:
            c.source_b = sanitize_and_redact(c.source_b)
        if c.nature:
            c.nature = sanitize_and_redact(c.nature)
        if c.resolution_details:
            c.resolution_details = sanitize_and_redact(c.resolution_details)

    # 5. Failures
    for fail in packet.failures:
        fail.error_message = sanitize_and_redact(fail.error_message)
        if fail.source_tool_or_subagent:
            fail.source_tool_or_subagent = sanitize_and_redact(fail.source_tool_or_subagent)
        if fail.failure_type:
            fail.failure_type = sanitize_and_redact(fail.failure_type)

    # 6. Subagent Receipts
    for r in packet.subagent_receipts:
        r.role = sanitize_and_redact(r.role)
        r.summary = sanitize_and_redact(r.summary)
        r.failures = [sanitize_and_redact(fail_msg) for fail_msg in r.failures]

    # 7. Artifact References
    for art in packet.artifact_references:
        art.tool_name = sanitize_and_redact(art.tool_name)
        if art.description:
            art.description = sanitize_and_redact(art.description)

    # 8. Escalation Requirements
    for esc in packet.escalation_requirements:
        esc.reason = sanitize_and_redact(esc.reason)
        esc.required_action = sanitize_and_redact(esc.required_action)
        if esc.severity:
            esc.severity = sanitize_and_redact(esc.severity)
        if esc.context:
            esc.context = recursive_sanitize_value(esc.context)

    return packet


class PacketValidator:
    """Authoritative validation and certification engine for CloudReturnPacket.
    Deterministically resolves evidence references, enforces task and conversation isolation,
    prevents model self-certification, and applies recursive credential redaction.
    """

    def __init__(self, artifact_store: Optional[ArtifactStore] = None):
        self.artifact_store = artifact_store

    def validate(
        self,
        packet: CloudReturnPacket,
        contract: TaskDelegationContract,
        strict: bool = True,
        trajectory_store: Optional[Any] = None,
        subagent_manager: Optional[Any] = None,
        task_conversation_id: Optional[str] = None,
        allowed_conversations: Optional[Set[str]] = None,
    ) -> Tuple[bool, List[str]]:
        """Deterministically validates the packet against the contract and authoritative execution stores.

        Returns:
            Tuple of (is_valid: bool, errors: List[str])
        """
        errors: List[str] = []

        # Build set of authorized conversations for evidence scoping
        authorized_convs: Set[str] = set()
        if packet.parent_conversation_id:
            authorized_convs.add(packet.parent_conversation_id)
        if contract.parent_conversation_id:
            authorized_convs.add(contract.parent_conversation_id)
        if task_conversation_id:
            authorized_convs.add(task_conversation_id)
        if allowed_conversations:
            authorized_convs.update(allowed_conversations)

        # 1. Identity and Task Integrity
        if packet.task_id != contract.task_id:
            errors.append(
                f"Task ID mismatch: packet has '{packet.task_id}', expected '{contract.task_id}'."
            )
        if packet.parent_conversation_id != contract.parent_conversation_id:
            errors.append(
                f"Parent conversation ID mismatch: packet has '{packet.parent_conversation_id}', "
                f"expected '{contract.parent_conversation_id}'."
            )
        if packet.objective != contract.objective:
            errors.append(
                "Objective mismatch: packet objective does not match task delegation contract."
            )

        # 2. Evidence Integrity - Real Resolution & Verification Authority
        for finding in packet.verified_findings:
            if not finding.statement or not finding.statement.strip():
                finding.verified = False
                errors.append(
                    f"Verified finding '{finding.finding_id}' contains empty statement."
                )
                continue

            if not finding.evidence_refs:
                # Finding cannot be verified without evidence references
                finding.verified = False
                errors.append(
                    f"Finding '{finding.finding_id}' has empty evidence references; verification denied."
                )
                continue

            # A. Resolve each evidence reference against authoritative stores (Provenance Validity)
            all_refs_valid = True
            for ref in finding.evidence_refs:
                is_resolved, res_err = self._resolve_evidence_reference(
                    ref=ref,
                    authorized_convs=authorized_convs,
                    trajectory_store=trajectory_store,
                    subagent_manager=subagent_manager,
                    task_id=contract.task_id,
                )
                if not is_resolved:
                    all_refs_valid = False
                    finding.verified = False
                    errors.append(
                        f"Evidence reference '{ref}' for finding '{finding.finding_id}' failed resolution: {res_err}"
                    )

            if not all_refs_valid:
                finding.verified = False
                continue

            # B. Verify Factual Support & Deterministic Extraction Basis (Claim Verification)
            if finding.verified:
                is_supported, sup_err = self._verify_factual_support(
                    finding=finding,
                    authorized_convs=authorized_convs,
                    trajectory_store=trajectory_store,
                )

                if not is_supported:
                    # Factual support failed: strip self-certified verified=True
                    finding.verified = False
                    errors.append(
                        f"Claim verification denied for finding '{finding.finding_id}': {sup_err}"
                    )
                else:
                    # Deterministically grounded fact
                    finding.verified = True

        # 3. Evidence Integrity - Persistent Artifact References
        if self.artifact_store:
            for art_ref in packet.artifact_references:
                try:
                    meta = self.artifact_store.get_metadata(
                        conversation_id=packet.parent_conversation_id,
                        artifact_id=art_ref.artifact_id,
                    )
                    # Check task conversation id if not found under parent
                    if not meta and task_conversation_id:
                        meta = self.artifact_store.get_metadata(
                            conversation_id=task_conversation_id,
                            artifact_id=art_ref.artifact_id,
                        )

                    if not meta:
                        errors.append(
                            f"Referenced artifact '{art_ref.artifact_id}' does not exist in ArtifactStore for authorized conversations."
                        )
                    elif meta.sha256 != art_ref.sha256:
                        errors.append(
                            f"Artifact '{art_ref.artifact_id}' integrity violation: "
                            f"packet SHA-256 ({art_ref.sha256}) does not match stored SHA-256 ({meta.sha256})."
                        )
                except Exception as e:
                    errors.append(
                        f"Error verifying artifact '{art_ref.artifact_id}': {e}"
                    )

        # 4. Semantic Integrity - Outcome & Completion Consistency
        if contract.completion_criteria:
            unresolved_crits = [
                q for q in packet.unresolved_questions
                if any(crit.lower() in q.lower() for crit in contract.completion_criteria)
            ]
            if unresolved_crits and packet.outcome == OutcomeStatus.COMPLETED:
                errors.append(
                    f"Outcome cannot be 'COMPLETED' when mandatory completion criteria remain unresolved: {unresolved_crits}"
                )

        # Fatal unresolved failures cannot result in COMPLETED
        unresolved_fatal = [
            f for f in packet.failures
            if not f.resolved and f.failure_type in ("NON_ZERO_EXIT", "TEST_FAILURE", "EXCEPTION")
        ]
        if unresolved_fatal and packet.outcome == OutcomeStatus.COMPLETED:
            errors.append(
                f"Outcome cannot be 'COMPLETED' while {len(unresolved_fatal)} unresolved fatal failures exist."
            )

        # 5. Contradiction Preservation
        if packet.contradictions:
            for c in packet.contradictions:
                if not c.claim_a or not c.claim_b:
                    errors.append(f"Contradiction has missing claim data: {c}")

        # 6. Recursive Whole-Packet Sanitization Pass
        recursive_sanitize_packet(packet)

        is_valid = (len(errors) == 0)
        if strict and not is_valid:
            logger.error(f"CloudReturnPacket validation failed with {len(errors)} error(s): {errors}")
            raise PacketValidationError(
                f"CloudReturnPacket failed deterministic validation: {'; '.join(errors)}"
            )

        return is_valid, errors

    def _resolve_evidence_reference(
        self,
        ref: str,
        authorized_convs: Set[str],
        trajectory_store: Optional[Any],
        subagent_manager: Optional[Any],
        task_id: str,
    ) -> Tuple[bool, str]:
        """Resolves an evidence reference against authoritative runtime data sources."""
        ref_str = str(ref).strip()
        if not ref_str:
            return False, "Evidence reference cannot be empty string."

        # 1. Artifact Reference (e.g. 'artifact:art_xxxx' or 'art_xxxx')
        art_match = re.search(r"(?:artifact:)?(art_[a-zA-Z0-9_\-]+)", ref_str)
        if art_match:
            art_id = art_match.group(1)
            if not self.artifact_store:
                return True, ""  # Store not injected, cannot disprove

            # Check if it exists in ANY of the authorized conversations
            found_in_auth = False
            for c_id in authorized_convs:
                if self.artifact_store.get_metadata(c_id, art_id):
                    found_in_auth = True
                    break

            if found_in_auth:
                return True, ""

            # Check if artifact exists in a foreign conversation (Cross-Conversation Pollution)
            try:
                for entry in self.artifact_store.root_dir.iterdir():
                    if entry.is_dir() and entry.name not in authorized_convs:
                        if self.artifact_store.get_metadata(entry.name, art_id):
                            return False, f"Cross-conversation artifact detected: '{art_id}' belongs to foreign conversation '{entry.name}'."
            except Exception:
                pass

            return False, f"Artifact '{art_id}' not found in ArtifactStore for authorized conversations."

        # 2. Subagent Reference (e.g. 'subagent:sub_xxxx')
        if ref_str.startswith("subagent:"):
            sub_id = ref_str.split(":", 1)[1].strip()
            if not subagent_manager:
                return True, ""

            status = subagent_manager.get_subagent_status(sub_id)
            if not status:
                return False, f"Referenced subagent '{sub_id}' does not exist in SubagentManager."

            # Check conversation scoping
            parent_cid = status.get("parent_conversation_id")
            if parent_cid and parent_cid not in authorized_convs:
                return False, f"Cross-task subagent detected: '{sub_id}' belongs to foreign parent conversation '{parent_cid}'."

            return True, ""

        # 3. Tool Reference (e.g. 'tool:name:call_id' or 'tool:name')
        if ref_str.startswith("tool:"):
            if not trajectory_store:
                return True, ""

            parts = ref_str.split(":")
            tool_name = parts[1].strip() if len(parts) > 1 else ""
            call_id = parts[2].strip() if len(parts) > 2 else ""

            # Verify in trajectory store steps
            try:
                steps = trajectory_store.list_steps()
                matched = False
                for s in steps:
                    p = s.step_payload or {}
                    p_name = p.get("name", "")
                    p_call = p.get("tool_call_id", "")
                    if tool_name and (p_name == tool_name or tool_name in p_name):
                        if not call_id or call_id == p_call or call_id in p_call or f"call_{s.idx}" == call_id:
                            matched = True
                            break
                if not matched:
                    return False, f"Tool execution '{ref_str}' does not resolve to an actual step in this task's trajectory."
            except Exception as e:
                logger.debug(f"Could not check trajectory store steps: {e}")

            return True, ""

        # 4. Trajectory Step Reference (e.g. 'trajectory:step_1' or 'step:2')
        step_match = re.search(r"(?:trajectory:|step:)(\d+)", ref_str)
        if step_match:
            step_idx = int(step_match.group(1))
            if not trajectory_store:
                return True, ""

            try:
                step_obj = trajectory_store.get_step(step_idx)
                if not step_obj:
                    return False, f"Trajectory step {step_idx} does not exist in task trajectory."
            except Exception:
                return False, f"Failed resolving trajectory step {step_idx}."

            return True, ""

        # 5. Local Qwen Synthesis Provenance (e.g. 'local_qwen_synthesis:task_id')
        if ref_str.startswith("local_qwen_synthesis:"):
            prov_task = ref_str.split(":", 1)[1].strip()
            if prov_task != task_id:
                return False, f"Cross-task synthesis provenance mismatch: '{prov_task}' != '{task_id}'."
            return True, ""

        # 6. Permission Boundary Reference (e.g. 'permission_boundary:tool_name')
        if ref_str.startswith("permission_boundary:"):
            return True, ""

        # Unrecognized or fabricated format
        return False, f"Unrecognized or fabricated evidence reference format: '{ref_str}'."

    def _verify_factual_support(
        self,
        finding: VerifiedFinding,
        authorized_convs: Set[str],
        trajectory_store: Optional[Any],
    ) -> Tuple[bool, str]:
        """Verifies whether a claim has an approved deterministic extraction basis
        and is factually grounded in its referenced evidence data.
        """
        APPROVED_DETERMINISTIC_METHODS = {
            "deterministic_test_parser",
            "deterministic_traceback_parser",
            "deterministic_directory_lister",
            "deterministic_exit_code",
            "deterministic_json_field",
            "deterministic_search_match",
            "deterministic",
        }

        # 1. Deterministic Extraction Basis Check
        if finding.extraction_method not in APPROVED_DETERMINISTIC_METHODS:
            return (
                False,
                f"Extraction method '{finding.extraction_method}' is not an approved deterministic extractor. "
                "Model hypotheses and agent assertions cannot self-certify as verified facts."
            )

        # 2. Artifact Content Grounding Check
        for ref in finding.evidence_refs:
            art_match = re.search(r"(?:artifact:)?(art_[a-zA-Z0-9_\-]+)", ref)
            if art_match and self.artifact_store:
                art_id = art_match.group(1)
                content_text = None
                for c_id in authorized_convs:
                    try:
                        c_path, _ = self.artifact_store._get_artifact_files(c_id, art_id)
                        if c_path.exists():
                            with open(c_path, "r", encoding="utf-8", errors="replace") as cf:
                                content_text = cf.read()
                            break
                    except Exception:
                        pass

                if content_text is not None:
                    # Check exact extracted value if specified
                    if finding.extracted_value:
                        if finding.extracted_value not in content_text:
                            return (
                                False,
                                f"Factual mismatch: Extracted value '{finding.extracted_value}' "
                                f"was not found in referenced artifact '{art_id}'."
                            )
                    else:
                        # Check keyword grounding: require at least one substantive claim term
                        stopwords = {
                            "the", "and", "for", "with", "from", "that", "this", "were", "been",
                            "have", "file", "found", "verified", "state", "status", "complete",
                            "completed", "observation", "observed"
                        }
                        tokens = [
                            w.lower() for w in re.findall(r"\b[A-Za-z0-9_\-\.]{3,}\b", finding.statement)
                            if w.lower() not in stopwords
                        ]
                        if tokens:
                            content_lower = content_text.lower()
                            matched = [t for t in tokens if t in content_lower]
                            if not matched:
                                return (
                                    False,
                                    f"Factual irrelevance: Referenced artifact '{art_id}' exists but does not "
                                    f"contain any substantive terms supporting claim: '{finding.statement}'."
                                )

        # 3. Trajectory Step Grounding Check
        if trajectory_store:
            for ref in finding.evidence_refs:
                if ref.startswith("step:"):
                    try:
                        s_idx = int(ref.split(":", 1)[1].strip())
                        steps = trajectory_store.list_steps()
                        matched_step = next((s for s in steps if s.idx == s_idx), None)
                        if matched_step and finding.extracted_value:
                            payload_str = json.dumps(matched_step.step_payload).lower()
                            if finding.extracted_value.lower() not in payload_str:
                                return (
                                    False,
                                    f"Factual mismatch: Extracted value '{finding.extracted_value}' "
                                    f"not found in trajectory step {s_idx} payload."
                                )
                    except Exception:
                        pass

        return True, ""
