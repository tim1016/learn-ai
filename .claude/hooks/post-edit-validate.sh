#!/bin/bash
# Post-edit validation hook — runs linter/compiler after file edits

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ -z "$FILE_PATH" ]]; then
  exit 0
fi

if [[ "$FILE_PATH" =~ \.(ts|html)$ ]]; then
  cd "$CLAUDE_PROJECT_DIR/Frontend" && npx tsc --noEmit 2>&1
elif [[ "$FILE_PATH" =~ \.py$ ]]; then
  ruff check "$FILE_PATH" 2>&1
elif [[ "$FILE_PATH" =~ \.cs$ ]]; then
  cd "$CLAUDE_PROJECT_DIR" && dotnet build --no-restore --nologo -v q 2>&1
fi

exit 0
