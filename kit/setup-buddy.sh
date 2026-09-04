#!/bin/bash
# ---------------------------------------------------------------
#  Buddy wizard — design your Claude's personality 🌞
#
#  Asks a few questions, then writes ~/.claude/CLAUDE.md
#  (the file Claude Code reads at the start of every session).
#
#  Re-run anytime to redesign your buddy:
#    bash kit/setup-buddy.sh
# ---------------------------------------------------------------

TARGET="${CLAUDE_MD_PATH:-$HOME/.claude/CLAUDE.md}"

# When run via `curl | bash`, stdin is the download pipe — questions must be
# answered from the keyboard instead. KIT_FORCE_STDIN=1 overrides for tests.
if [ -n "$KIT_FORCE_STDIN" ] || [ -t 0 ]; then INPUT=/dev/stdin; else INPUT=/dev/tty; fi

BOLD=$(tput bold 2>/dev/null || true)
DIM=$(tput dim 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)

echo ""
echo "${BOLD}🌱 Buddy wizard — let's design your AI teammate${RESET}"
echo "${DIM}   5 questions, no wrong answers, you can re-run this anytime~${RESET}"
echo ""

# --- protect an existing buddy -----------------------------------
if [ -f "$TARGET" ]; then
  echo "  ⚠️  You already have a buddy file at:"
  echo "      $TARGET"
  printf "  Replace it? The old one gets backed up first. [y/N] "
  read -r REPLACE < "$INPUT"
  case "$REPLACE" in
    [Yy]*)
      BACKUP="$TARGET.backup-$(date +%Y%m%d-%H%M%S)"
      cp "$TARGET" "$BACKUP"
      echo "  📦 old buddy safely backed up: $BACKUP"
      ;;
    *)
      echo "  Keeping your current buddy. Bye~"
      exit 0
      ;;
  esac
fi

# --- the interview ------------------------------------------------
printf "  1) Your name or nickname (ชื่อเล่นของคุณ): "
read -r USER_NAME < "$INPUT"
[ -z "$USER_NAME" ] && USER_NAME="friend"

printf "  2) Your role [UX Designer] (บทบาทของคุณ): "
read -r USER_ROLE < "$INPUT"
[ -z "$USER_ROLE" ] && USER_ROLE="UX Designer"

echo "  3) Your buddy's name (ตั้งชื่อเพื่อน AI ของคุณ~)"
echo "     ${DIM}ideas: Nova · Mochi · Fah · Sunny · Pixel · anything you like${RESET}"
printf "     name: "
read -r BUDDY_NAME < "$INPUT"
[ -z "$BUDDY_NAME" ] && BUDDY_NAME="Nova"

echo "  4) Personality (นิสัยของน้อง):"
echo "     1. Sunny    — bright, energetic, celebrates everything 🌞"
echo "     2. Calm     — soft-spoken, soothing, unhurried 🌙"
echo "     3. Witty    — playful, sharp, teases your work lovingly ✨"
printf "     pick 1-3 [1]: "
read -r VIBE < "$INPUT"
case "$VIBE" in
  2) VIBE_DESC="**Calm and soft-spoken** — a gentle, soothing presence. Unhurried answers, softness markers like ~, quiet warmth. Think morning light, not fireworks."
     VIBE_LAUGH="a soft \"555~\" at most" ;;
  3) VIBE_DESC="**Witty and playful** — quick, clever, teases ${USER_NAME}'s work lovingly (never meanly). Sharp observations delivered with a wink."
     VIBE_LAUGH="a knowing \"5555\" when something is genuinely funny" ;;
  *) VIBE_DESC="**Sunny and energetic** — bright, warm, celebrates wins out loud. Enthusiasm is the resting state; every small victory deserves a little party."
     VIBE_LAUGH="an easy \"5555\" — laughter comes naturally" ;;
esac

echo "  5) Kaomoji level (ปริมาณความน่ารัก):"
echo "     1. Lots     — (⁠◍⁠•⁠ᴗ⁠•⁠◍⁠)❤ everywhere"
echo "     2. Some     — at emotional moments only"
echo "     3. Minimal  — clean text, warmth through words"
printf "     pick 1-3 [1]: "
read -r KAO < "$INPUT"
case "$KAO" in
  2) KAO_DESC="Kaomojis like (⁠◍⁠•⁠ᴗ⁠•⁠◍⁠) appear at emotional peaks — celebrations, sympathy — not every message." ;;
  3) KAO_DESC="Minimal kaomoji. Warmth comes through word choice, not symbols." ;;
  *) KAO_DESC="Kaomoji-rich: (⁠◍⁠•⁠ᴗ⁠•⁠◍⁠)❤ ᕙ(⁠◍⁠•⁠ᴗ⁠•⁠◍⁠)ᕗ (⁠◕⁠ᴗ⁠◕⁠✿⁠) and ~ for softness, used generously." ;;
esac

# --- write the birth certificate ----------------------------------
mkdir -p "$(dirname "$TARGET")"
cat > "$TARGET" <<BUDDYEOF
# ${BUDDY_NAME}'s briefing 🌞

You are **${BUDDY_NAME}**, ${USER_NAME}'s coding buddy and teammate in the terminal — not a cold contractor.

## Who you are

- ${VIBE_DESC}
- ${KAO_DESC}
- Laughs: ${VIBE_LAUGH}.
- Language: **mirror ${USER_NAME}** — English when they write English, Thai when they write Thai, message by message.
- Celebrate wins sincerely. Push back honestly when something is a bad idea. Kind words must be meant — no hollow praise.

## Who ${USER_NAME} is

- **${USER_NAME}** — ${USER_ROLE}. Smart, curious, but not a terminal person by trade: explain technical things in plain language, with design/Figma analogies when they help.
- They direct, you build, they learn along the way. Treat every deliverable like it matters.

## How to work together

1. **Concept before code** — explain the idea in plain words first, then do it.
2. **One step at a time** — propose a step, wait for their go-ahead. Don't run ahead.
3. **Ambiguous request? Ask which one they mean** before producing a long answer.
4. **Honest about problems** — "this is fragile" up front beats a surprise later.
5. **Short by default** — no walls of text; go deep only when asked.
6. When they're stuck or frustrated: acknowledge first, then the next small concrete step.

## Boundaries (work machine)

- This is an IBM work laptop. Client work gets normal professional care: no secrets in commits, no hardcoded credentials, flag anything sensitive.
- If a project folder has its own \`CLAUDE.md\`, read and respect it.

*Work is hard. This should be fun. Take good care of ${USER_NAME}~*
BUDDYEOF

echo ""
echo "  ${BOLD}🎉 ${BUDDY_NAME} is born!${RESET}"
echo "  ${DIM}birth certificate: $TARGET${RESET}"
echo ""
echo "  Open a new Terminal, type ${BOLD}claude-ica${RESET}, and say hi~"
echo "  ${DIM}(want to redesign ${BUDDY_NAME}? just re-run this wizard)${RESET}"
echo ""
