"""Tests for document sending functionality across all layers."""

import json
import os
import sys
import importlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _import_send_message_tool():
    """Import send_message_tool directly, bypassing tools/__init__.py (needs firecrawl)."""
    spec = importlib.util.spec_from_file_location(
        "send_message_tool",
        PROJECT_ROOT / "tools" / "send_message_tool.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Pre-populate the tools.registry dependency so the registry import inside the
    # module works without importing tools/__init__.py.
    if "tools.registry" not in sys.modules:
        reg_spec = importlib.util.spec_from_file_location(
            "tools.registry",
            PROJECT_ROOT / "tools" / "registry.py",
        )
        reg_mod = importlib.util.module_from_spec(reg_spec)
        sys.modules["tools.registry"] = reg_mod
        reg_spec.loader.exec_module(reg_mod)
    # Pre-populate tools.interrupt
    if "tools.interrupt" not in sys.modules:
        int_spec = importlib.util.spec_from_file_location(
            "tools.interrupt",
            PROJECT_ROOT / "tools" / "interrupt.py",
        )
        int_mod = importlib.util.module_from_spec(int_spec)
        sys.modules["tools.interrupt"] = int_mod
        int_spec.loader.exec_module(int_mod)
    spec.loader.exec_module(mod)
    return mod


_smt = _import_send_message_tool()
SEND_MESSAGE_SCHEMA = _smt.SEND_MESSAGE_SCHEMA
_validate_file_path = _smt._validate_file_path
send_message_tool = _smt.send_message_tool


# ── Schema Tests ──────────────────────────────────────────────────────────

class TestSendMessageSchema:
    """Verify the send_message tool schema includes file_path."""

    def test_schema_has_file_path(self):
        props = SEND_MESSAGE_SCHEMA["parameters"]["properties"]
        assert "file_path" in props
        assert props["file_path"]["type"] == "string"

    def test_schema_file_path_description(self):
        desc = SEND_MESSAGE_SCHEMA["parameters"]["properties"]["file_path"]["description"]
        assert "document" in desc.lower() or "file" in desc.lower()


# ── Validation Tests ─────────────────────────────────────────────────────

class TestFilePathValidation:
    """Test _validate_file_path security and correctness."""

    def test_validate_none_path(self):
        assert _validate_file_path("") is None
        assert _validate_file_path(None) is None

    def test_validate_path_traversal_rejected(self):
        result = _validate_file_path("/tmp/../etc/passwd")
        assert result is not None
        assert "traversal" in result.lower()

    def test_validate_nonexistent_file(self):
        result = _validate_file_path("/tmp/nonexistent_file_abc123.pdf")
        assert result is not None
        assert "not found" in result.lower()

    def test_validate_directory_rejected(self):
        result = _validate_file_path("/tmp")
        assert result is not None
        assert "not a file" in result.lower()

    def test_validate_existing_file_ok(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test content")
            f.flush()
            try:
                result = _validate_file_path(f.name)
                assert result is None  # No error
            finally:
                os.unlink(f.name)


# ── send_message_tool Integration Tests ──────────────────────────────────

class TestSendMessageToolWithFile:
    """Test send_message tool handler with file_path."""

    def test_send_no_target_returns_error(self):
        result = json.loads(send_message_tool({"action": "send", "file_path": "/tmp/test.pdf"}))
        assert "error" in result

    def test_send_no_message_no_file_returns_error(self):
        result = json.loads(send_message_tool({"action": "send", "target": "telegram:123"}))
        assert "error" in result

    def test_send_file_not_found_returns_error(self):
        result = json.loads(send_message_tool({
            "action": "send",
            "target": "telegram:123",
            "file_path": "/nonexistent/file.pdf"
        }))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_send_file_path_traversal_returns_error(self):
        result = json.loads(send_message_tool({
            "action": "send",
            "target": "telegram:123",
            "file_path": "/tmp/../etc/passwd"
        }))
        assert "error" in result
        assert "traversal" in result["error"].lower()


# ── extract_documents Tests ──────────────────────────────────────────────

class TestExtractDocuments:
    """Test DOCUMENT: tag extraction from response text."""

    def test_extract_single_document(self):
        from gateway.platforms.base import BasePlatformAdapter
        content = "Here is the file:\nDOCUMENT:/tmp/report.pdf\nDone."
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert docs[0][0] == "/tmp/report.pdf"
        assert docs[0][1] == ""
        assert "DOCUMENT:" not in cleaned
        assert "Done." in cleaned

    def test_extract_document_with_caption(self):
        from gateway.platforms.base import BasePlatformAdapter
        content = "DOCUMENT:/tmp/report.pdf|Monthly revenue report"
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert docs[0][0] == "/tmp/report.pdf"
        assert docs[0][1] == "Monthly revenue report"

    def test_extract_multiple_documents(self):
        from gateway.platforms.base import BasePlatformAdapter
        content = "Files:\nDOCUMENT:/tmp/a.pdf\nDOCUMENT:/tmp/b.xlsx\n"
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 2
        assert docs[0][0] == "/tmp/a.pdf"
        assert docs[1][0] == "/tmp/b.xlsx"

    def test_extract_no_documents(self):
        from gateway.platforms.base import BasePlatformAdapter
        content = "No documents here, just text."
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 0
        assert cleaned == content

    def test_extract_documents_cleans_blank_lines(self):
        from gateway.platforms.base import BasePlatformAdapter
        content = "Before\n\n\nDOCUMENT:/tmp/file.pdf\n\n\n\nAfter"
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert "\n\n\n" not in cleaned


# ── BasePlatformAdapter.send_document Fallback Test ──────────────────────

class TestBaseSendDocumentFallback:
    """Test the base class fallback for send_document."""

    @pytest.mark.asyncio
    async def test_fallback_sends_text(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult
        from gateway.config import Platform, PlatformConfig

        class StubAdapter(BasePlatformAdapter):
            async def connect(self): return True
            async def disconnect(self): pass
            async def send(self, chat_id, content, reply_to=None, metadata=None):
                self._last_sent = content
                return SendResult(success=True, message_id="1")
            async def get_chat_info(self, chat_id):
                return {"name": "test", "type": "dm"}

        config = PlatformConfig(token="test", enabled=True)
        adapter = StubAdapter(config, Platform.TELEGRAM)
        result = await adapter.send_document("123", "/tmp/test.pdf", caption="Here you go")
        assert result.success
        assert "📎" in adapter._last_sent
        assert "/tmp/test.pdf" in adapter._last_sent
        assert "Here you go" in adapter._last_sent


# ── TelegramAdapter.send_document Override Test ──────────────────────────

class TestTelegramSendDocument:
    """Test TelegramAdapter.send_document sends via Bot API."""

    @pytest.mark.asyncio
    async def test_send_document_calls_bot(self):
        from gateway.platforms.telegram import TelegramAdapter
        from gateway.config import PlatformConfig

        config = PlatformConfig(token="test-token", enabled=True)
        adapter = TelegramAdapter(config)

        # Mock the bot
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        mock_bot = AsyncMock()
        mock_bot.send_document = AsyncMock(return_value=mock_msg)
        adapter._bot = mock_bot

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test content")
            f.flush()
            try:
                result = await adapter.send_document("12345", f.name, caption="Test doc")
                assert result.success
                assert result.message_id == "42"
                mock_bot.send_document.assert_called_once()
                call_kwargs = mock_bot.send_document.call_args
                assert call_kwargs.kwargs["chat_id"] == 12345
                assert call_kwargs.kwargs["caption"] == "Test doc"
                assert call_kwargs.kwargs["filename"] == os.path.basename(f.name)
            finally:
                os.unlink(f.name)

    @pytest.mark.asyncio
    async def test_send_document_file_not_found(self):
        from gateway.platforms.telegram import TelegramAdapter
        from gateway.config import PlatformConfig

        config = PlatformConfig(token="test-token", enabled=True)
        adapter = TelegramAdapter(config)
        adapter._bot = AsyncMock()

        result = await adapter.send_document("12345", "/nonexistent/file.pdf")
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_document_not_connected(self):
        from gateway.platforms.telegram import TelegramAdapter
        from gateway.config import PlatformConfig

        config = PlatformConfig(token="test-token", enabled=True)
        adapter = TelegramAdapter(config)
        # _bot is None (not connected)

        result = await adapter.send_document("12345", "/tmp/test.pdf")
        assert not result.success
        assert "not connected" in result.error.lower()
