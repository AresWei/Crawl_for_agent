"""Product bounds around the original C4A grammar, IR passes and emitters."""
from __future__ import annotations

import json
from pathlib import Path

from .c4ai_script import ASTBuilder, Cmd, Compiler as SourceCompiler


class Compiler(SourceCompiler):
    def __init__(self, root=None):
        super().__init__(root)
        self.includes_allowed = root is not None
        self._source_bytes = 0
        self._expansion_stack = []

    def _parse_with_includes(self, text, seen=None):
        self._source_bytes += len(text.encode('utf-8'))
        if self._source_bytes > 128_000:
            raise ValueError('C4A source exceeds 128000 bytes')
        seen = set() if seen is None else seen
        commands = ASTBuilder().transform(self.parser.parse(text))
        out = []
        for command in commands:
            if isinstance(command, Cmd) and command.op == 'INCLUDE':
                if not self.includes_allowed:
                    raise ValueError('USE requires an explicit include root')
                path = (self.root / command.args[0]).resolve()
                if not path.is_relative_to(self.root):
                    raise ValueError('USE path must remain under the include root')
                if path in seen or len(seen) >= 32:
                    raise ValueError('Circular or excessive C4A includes')
                out.extend(self._parse_with_includes(path.read_text(encoding='utf-8-sig'), seen | {path}))
            else:
                out.append(command)
        return out

    def _inline_calls(self, ir):
        out = []
        for command in ir:
            if isinstance(command, Cmd) and command.op == 'CALL':
                name = command.args[0]
                if name not in self.procs:
                    raise ValueError(f'Unknown procedure {name!r}')
                if name in self._expansion_stack or len(self._expansion_stack) >= 32:
                    raise ValueError('Recursive or excessive procedure expansion')
                self._expansion_stack.append(name)
                try:
                    out.extend(self._inline_calls(self.procs[name].body))
                finally:
                    self._expansion_stack.pop()
            else:
                out.append(command)
            if len(out) > 2000:
                raise ValueError('C4A exceeds 2000 compiled statements')
        return out

    def _handle_cmd_or_proc(self, command):
        if command is None:
            return ''
        if command.op == 'CALL':
            name = command.args[0]
            if name not in self.procs:
                raise ValueError(f'Unknown procedure {name!r}')
            if name in self._expansion_stack or len(self._expansion_stack) >= 32:
                raise ValueError('Recursive or excessive procedure expansion')
            self._expansion_stack.append(name)
            try:
                return '\n'.join(self._handle_cmd_or_proc(c) for c in self.procs[name].body if c.op != 'NOP')
            finally:
                self._expansion_stack.pop()
        return self._emit_js(command)

    def _emit_js(self, command):
        # Upstream catches and logs EVAL errors, hiding failed actions from results.
        if command.op == 'EVAL':
            return f'await (async () => {{ {command.args[0]}\n }})();'
        if command.op == 'GO':
            return f'window.location.href = {json.dumps(command.args[0])};'
        if command.op == 'REPEAT':
            action, count = command.args
            expression = count[1:-1] if count.startswith('`') else count
            action_js = self._handle_cmd_or_proc(action)
            # Preserve the source numeric count/truthy once semantics, with a bound
            # and asynchronous body so WAIT works in expression-count repeats.
            return f'''await (async () => {{
              const count = ({expression});
              const n = typeof count === 'number' ? Math.ceil(Math.max(0,count)) : (count ? 1 : 0);
              if (!Number.isFinite(n) || n > 1000) throw new Error('C4A repeat limit');
              for (let i=0;i<n;i++) {{ {action_js} }}
            }})();'''
        return super()._emit_js(command)
