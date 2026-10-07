"""Stream top-level hook metadata; large prompts/images never enter the event bus.

The IDE owns the JSON producer. Discarded values are scanned structurally rather
than decoded/retained; selected values still pass the standard JSON decoder.
"""
import json
import re

LIMIT = 64 * 1024 * 1024
VALUE_LIMIT = 32 * 1024
FIELDS = {
    "hook_event_name", "conversation_id", "generation_id", "session_id", "turn_id",
    "parent_conversation_id", "parent_session_id", "parent_id", "tool_use_id",
    "tool_call_id", "subagent_id", "agent_id", "tool_name", "agent_type", "subagent_type",
    "status", "end_reason", "reason", "stop_reason", "workspace_roots", "cwd",
}
STRUCTURE = re.compile(rb'["\\{}\[\]]')


class InputLimit(ValueError):
    pass


class MetadataReader:
    def __init__(self, stream):
        self.stream, self.buffer, self.at, self.total = stream, b"", 0, 0

    def fill(self):
        if self.at < len(self.buffer):
            return True
        self.buffer = self.stream.read(65536)
        if isinstance(self.buffer, str):
            self.buffer = self.buffer.encode("utf-8")
        self.at = 0
        self.total += len(self.buffer)
        if self.total > LIMIT:
            raise InputLimit("Hook input exceeds streaming limit")
        return bool(self.buffer)

    def peek(self):
        return self.buffer[self.at] if self.fill() else None

    def byte(self):
        value = self.peek()
        if value is None:
            raise ValueError("Incomplete hook input")
        self.at += 1
        return value

    def whitespace(self):
        while self.peek() in (9, 10, 13, 32):
            self.at += 1

    def value(self, keep):
        self.whitespace()
        first = self.byte()
        saved = bytearray([first]) if keep else None

        def add(data):
            if saved is not None:
                saved.extend(data)
                if len(saved) > VALUE_LIMIT:
                    raise InputLimit("Hook metadata value exceeds limit")

        if first in (34, 91, 123):
            stack = [93 if first == 91 else 125] if first != 34 else []
            string = first == 34
            while True:
                if not self.fill():
                    raise ValueError("Incomplete JSON value")
                match = STRUCTURE.search(self.buffer, self.at)
                stop = match.end() if match else len(self.buffer)
                add(self.buffer[self.at:stop])
                self.at = stop
                if not match:
                    continue
                token = match.group()[0]
                if string:
                    if token == 92:
                        add(bytes([self.byte()]))
                    elif token == 34:
                        string = False
                        if not stack:
                            break
                elif token == 34:
                    string = True
                elif token in (91, 123):
                    stack.append(93 if token == 91 else 125)
                elif token in (93, 125):
                    if not stack or stack.pop() != token:
                        raise ValueError("Mismatched JSON brackets")
                    if not stack:
                        break
        else:
            while self.peek() not in (None, 9, 10, 13, 32, 44, 125):
                add(bytes([self.byte()]))
        return json.loads(saved) if saved is not None else None

    def read(self):
        self.whitespace()
        if self.peek() is None:
            return {}, "empty"
        if self.peek() == 239:  # UTF-8 BOM from a Windows pipe/file.
            if bytes([self.byte(), self.byte(), self.byte()]) != b"\xef\xbb\xbf":
                raise ValueError("Invalid hook encoding")
        if self.byte() != 123:
            raise ValueError("Hook input must be an object")
        result = {}
        while True:
            self.whitespace()
            if self.peek() == 125:
                self.byte()
                break
            if self.peek() != 34:
                raise ValueError("Invalid JSON key")
            key = self.value(True)
            if not isinstance(key, str):
                raise ValueError("Invalid hook key")
            self.whitespace()
            if self.byte() != 58:
                raise ValueError("Missing JSON colon")
            value = self.value(key in FIELDS)
            # Keep discarded field names for existing safe diagnostics, not values.
            result[key] = value
            self.whitespace()
            separator = self.byte()
            if separator == 125:
                break
            if separator != 44:
                raise ValueError("Missing JSON separator")
        self.whitespace()
        if self.peek() is not None:
            raise ValueError("Trailing hook data")
        return result, "ok"


def read_metadata(stream):
    reader = MetadataReader(stream)
    try:
        payload, status = reader.read()
        return payload, status, reader.total
    except InputLimit:
        return {}, "too_large", reader.total
    except (ValueError, UnicodeError, OSError):
        return {}, "invalid_json", reader.total
