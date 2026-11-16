"""Automated Unit Tests for Conversation Identity & Metadata"""

import unittest
from runtime.conversation import ConversationMetadata


class TestConversation(unittest.TestCase):
    """Validates ConversationMetadata behavior, serialization, and lineage."""

    def test_metadata_creation_and_defaults(self):
        meta = ConversationMetadata(
            title="Desktop File Assistant",
            workspace_path=r"C:\Users\test\project"
        )
        self.assertIsNotNone(meta.conversation_id)
        self.assertEqual(meta.title, "Desktop File Assistant")
        self.assertEqual(meta.nesting_depth, 0)
        self.assertEqual(meta.parent_conversation_id, "")
        self.assertEqual(meta.status, "ACTIVE")
        self.assertEqual(meta.workspace_mode, "inherit")

    def test_dict_serialization_and_deserialization(self):
        meta = ConversationMetadata(
            title="Research Subagent",
            workspace_path=r"C:\Users\test\workspace",
            workspace_mode="branch",
            status="IDLE"
        )
        d = meta.to_dict()
        self.assertEqual(d["title"], "Research Subagent")
        self.assertEqual(d["workspace_mode"], "branch")

        reconstructed = ConversationMetadata.from_dict(d)
        self.assertEqual(reconstructed.conversation_id, meta.conversation_id)
        self.assertEqual(reconstructed.title, meta.title)
        self.assertEqual(reconstructed.workspace_mode, "branch")
        self.assertEqual(reconstructed.status, "IDLE")

    def test_spawn_child_lineage(self):
        parent = ConversationMetadata(
            title="Root Agent",
            workspace_path=r"C:\workspace\root"
        )
        child = parent.spawn_child(
            title="Child Specialist",
            workspace_mode="branch"
        )
        self.assertNotEqual(child.conversation_id, parent.conversation_id)
        self.assertEqual(child.parent_conversation_id, parent.conversation_id)
        self.assertEqual(child.nesting_depth, 1)
        self.assertEqual(child.workspace_mode, "branch")
        self.assertEqual(child.workspace_path, parent.workspace_path)

        grandchild = child.spawn_child("Deep Specialist")
        self.assertEqual(grandchild.parent_conversation_id, child.conversation_id)
        self.assertEqual(grandchild.nesting_depth, 2)


if __name__ == "__main__":
    unittest.main()
