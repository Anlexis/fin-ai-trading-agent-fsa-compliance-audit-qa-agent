# FIN-C2-196 — Unit Tests: manifest / runtime-config consistency
#
# Two files, two jobs:
#
#   config/agent.yaml   the static manifest the registry reads for discovery.
#                       Flat — every key at root level, no nested blocks.
#   config/config.yaml  the runtime parameters, forwarded into the inner graph
#                       by ComplianceAuditGraphNode._parent_config().
#
# Both are live configuration rather than documentation, so these tests pin
# config ↔ code consistency: the declared class must BE the graph class, and
# the declared retrieval tuning must be the tuning the nodes actually use.
#
# Mirrors docs/03_test_spec.md §2.8 (CFG-01..CFG-07).
# Deterministic — no model call, no network.

import json
import pathlib

import yaml

from framework.schemas.trust_level import TrustLevel

from src.graph.graph import ComplianceAuditGraphNode, FSATradingComplianceAuditAgent
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = yaml.safe_load((_ROOT / "config" / "agent.yaml").read_text(encoding="utf-8"))
_RUNTIME = yaml.safe_load((_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))


class TestManifestIdentity:
    def test_cfg_01_manifest_is_flat_and_identifies_the_template(self):
        assert _MANIFEST["id"] == "FIN-C2-196"
        # The registry reads every key at root level; a nested block would be
        # silently ignored, taking the values inside it with it.
        assert "agent" not in _MANIFEST

    def test_cfg_02_declared_class_is_the_graph_class(self):
        # The declared entry point must BE the graph class the server imports.
        assert _MANIFEST["class"] == "src.graph.graph.FSATradingComplianceAuditAgent"
        assert _MANIFEST["name"] == FSATradingComplianceAuditAgent().name

    def test_cfg_03_category_and_industry(self):
        assert _MANIFEST["category"] == "Cat 2"
        assert _MANIFEST["industry"] == "FIN"
        assert _MANIFEST["base_type"] == "RAGAgent"

    def test_cfg_03b_declares_no_secrets_or_extras(self):
        # Both are code-derived: this template calls no secrets API and builds
        # no model client. Declaring either would make the agent fail to
        # compile at deploy time waiting for something nothing asks for.
        assert _MANIFEST["requires"]["secrets"] == []
        assert _MANIFEST["requires"]["extras"] == []
        assert _MANIFEST["generation_mode"] == "deterministic"


class TestManifestSecurity:
    def test_cfg_04_required_trust_level_matches_outer_gate_nodes(self):
        declared = TrustLevel(_MANIFEST["required_trust_level"])
        assert declared is TrustLevel.VERIFIED_EXTERNAL
        assert PreProcessNode.required_trust_level is declared
        assert PostProcessNode.required_trust_level is declared

    def test_cfg_05_max_retry_within_framework_ceiling(self):
        max_retry = _RUNTIME["max_retry"]
        assert isinstance(max_retry, int)
        assert 0 <= max_retry < 10  # framework retry ceiling

    def test_hitl_is_not_enabled(self):
        # This template declares no human-in-the-loop step, in either file.
        assert (_MANIFEST.get("hitl") or {}).get("enabled", False) is False
        assert (_RUNTIME.get("hitl") or {}).get("enabled", False) is False


class TestRetrievalBlock:
    def test_cfg_06_retrieval_block_matches_node_defaults(self):
        # Node module defaults mirror the runtime config — a drift silently
        # changes tuning.
        retrieval = _RUNTIME["retrieval"]
        from src.nodes.retrieve_node import _DEFAULT_RETRIEVAL as retrieve_defaults
        from src.nodes.rerank_filter_node import _DEFAULT_RETRIEVAL as rerank_defaults

        assert retrieval["top_k"] == retrieve_defaults["top_k"] == rerank_defaults["top_k"]
        assert (
            retrieval["score_threshold"] == retrieve_defaults["score_threshold"] == rerank_defaults["score_threshold"]
        )
        assert retrieval["kb_path"] == retrieve_defaults["kb_path"]
        assert (_ROOT / retrieval["kb_path"]).is_file()

    def test_cfg_07_parent_config_forwards_the_runtime_retrieval_block(self):
        # Reads config/config.yaml — the file the values actually live in.
        # Pointing this reader at the flat manifest instead would return
        # nothing and every declared value would be replaced by the fallback,
        # leaving a configuration that looks live and is not.
        cfg = ComplianceAuditGraphNode()._parent_config()
        assert cfg["configurable"]["retrieval"] == _RUNTIME["retrieval"]
        assert cfg["configurable"]["retrieval"], "_parent_config() must never forward an empty retrieval block"

    def test_cfg_08_runtime_config_declares_nothing_unread(self):
        # Every key shipped in config/config.yaml has a reader. A declared
        # block nobody reads is a promise the code does not keep.
        assert set(_RUNTIME) == {"max_retry", "timeout_s", "retrieval"}


class TestSeededKnowledgeBase:
    def test_kb_is_a_well_formed_entry_list(self):
        entries = json.loads((_ROOT / _RUNTIME["retrieval"]["kb_path"]).read_text(encoding="utf-8"))
        assert isinstance(entries, list)
        assert len(entries) >= 5, "seeded KB must carry a usable corpus"
        for entry in entries:
            assert set(entry.keys()) == {"id", "title", "category", "source", "tags", "content"}
            assert entry["id"] and entry["title"] and entry["content"]

    def test_kb_ids_are_unique(self):
        entries = json.loads((_ROOT / _RUNTIME["retrieval"]["kb_path"]).read_text(encoding="utf-8"))
        ids = [e["id"] for e in entries]
        assert len(ids) == len(set(ids))

    def test_kb_covers_the_four_fixed_topics(self):
        # docs/02_design.md: exactly 4 fixed FSA topic slugs back the classifier.
        entries = json.loads((_ROOT / _RUNTIME["retrieval"]["kb_path"]).read_text(encoding="utf-8"))
        categories = {e["category"] for e in entries}
        assert categories == {
            "definitive_judgment",
            "mrm_documentation",
            "incident_notification",
            "human_oversight",
        }
