#!/usr/bin/env bash
# AoE Chief demo: create a small repository with bugs and delegate it as a task.
# State lives in a separate directory, so your real board is untouched.
#   ./demo.sh           create the repository and start the task
#   ./demo.sh clean     remove the demo sessions, worktree, branch, repository and state
set -euo pipefail
REPO=~/workspace/chief-demo
export AOE_CHIEF_HOME=~/.local/state/aoe-chief-demo
C="python3 $(cd "$(dirname "$0")" && pwd)/chief.py"
AOE=~/.local/bin/aoe

if [ "${1:-}" = clean ]; then
  for f in "$AOE_CHIEF_HOME"/tasks/*/task.json; do
    [ -f "$f" ] || continue
    python3 -c "import json,sys;t=json.load(open(sys.argv[1]));print(t.get('session_id') or '');[print(p['id']) for p in t.get('past_sessions',[])]" "$f" |
      while read -r sid; do [ -n "$sid" ] && $AOE -p main remove "$sid" --purge --delete-worktree --delete-branch --force >/dev/null 2>&1 || true; done
  done
  rm -rf "$REPO" "${REPO}-worktrees" "$AOE_CHIEF_HOME"
  echo "Demo environment removed"
  exit 0
fi

[ -e "$REPO" ] && { echo "$REPO already exists; run ./demo.sh clean first"; exit 1; }
mkdir -p "$REPO/src" "$REPO/tests"
cd "$REPO"
git init -q -b main
cat > src/shop.py <<'EOF'
def total(prices):
    """Sum of prices."""
    return max(prices) if prices else 0


def apply_discount(amount, percent):
    """Amount after a percent discount, e.g. (200, 10) -> 180."""
    return amount - percent


def receipt(items):
    """Human-readable receipt for [(name, price), ...]. Format not decided yet."""
    raise NotImplementedError
EOF
cat > tests/test_shop.py <<'EOF'
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import shop

class T(unittest.TestCase):
    def test_total(self):
        self.assertEqual(shop.total([3, 4, 5]), 12)

    def test_discount(self):
        self.assertEqual(shop.apply_discount(200, 10), 180)

    def test_receipt(self):
        out = shop.receipt([("tea", 3), ("cake", 5)])
        self.assertIn("tea", out)
        self.assertIn("8", out)

if __name__ == "__main__":
    unittest.main()
EOF
git add -A
git -c user.name=demo -c user.email=demo@local commit -qm "init demo"

$C new --repo "$REPO" --name "shop demo" \
  --goal "Make all tests pass by fixing src/shop.py. Work in small steps: fix exactly ONE failing test per turn, commit, and end the turn. Before implementing receipt(), ask in Decision needed whether the receipt should be plain text lines or JSON (with your recommendation), and wait for the answer." \
  --check "python3 -m unittest discover -s tests -q" --scope src --protected tests --rounds 10 --hours 1 --trust     # throwaway directory: answer Codex's trust prompt
echo
echo "Task started. Next (after: export AOE_CHIEF_HOME=$AOE_CHIEF_HOME):"
echo "  $C watch --every 30      # let Chief drive the task"
echo "  $C brief                 # see what needs you"
echo "  $C decisions             # answer with: decide <id> --accept | --reply \"...\""
