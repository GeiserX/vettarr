#!/usr/bin/env bash
# The release gate: no tracked file and no commit message may carry a word
# from a private list.
#
#   scripts/leak-gate.sh <words-file> [<revision range>]
#   scripts/leak-gate.sh --self-test <words-file>
#
# <words-file> holds one case-insensitive Perl regular expression per line;
# blank lines and lines starting with # are skipped. A line that is one plain
# word (letters only) matches whole words only, so `cat` never matches
# `category`; an underscore and a change from lower to upper case end a word
# too, so `cat_food.txt` and `CatHD` both name `cat`. Any other line (a phrase,
# digits, regex syntax) matches as written; end it with \w* to match the start
# of longer words.
#
# Every tracked file is searched with `git grep -P -i -I` (binary files are
# skipped), and every commit message in <revision range> (default HEAD, the
# whole history) with the same patterns. A hit prints as `path:line` or
# `commit <sha>:line` and the numbers of the patterns' lines in the words
# file. The line itself never prints: neither the words nor whatever sits
# beside them (a passkey in a URL, say).
# Exit 0 when nothing matches, 1 on any hit, 2 when the gate cannot run.
#
# --self-test proves the gate can fail: in a scratch clone it plants one
# canary line per pattern into a new tracked file and the commit message that
# adds it, runs the gate there, and exits 0 only if the gate exits 1 and
# reports every canary. The words file is never copied into the repository.
set -euo pipefail

die() { echo "leak-gate: $*" >&2; exit 2; }

# git grep -P and perl read the files as UTF-8 with a UTF-8 locale.
for loc in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8; do
  if locale -a 2>/dev/null | grep -qx "$loc"; then export LC_ALL="$loc"; break; fi
done

self_test=0
if [ "${1:-}" = "--self-test" ]; then self_test=1; shift; fi
words="${1:-}"
[ -n "$words" ] || die "usage: $0 [--self-test] <words-file> [<revision range>]"
[ -r "$words" ] || die "cannot read the words file"
words="$(cd "$(dirname "$words")" && pwd -P)/$(basename "$words")"
range="${2:-HEAD}"
top="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git checkout"
self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The patterns as the gate runs them, one per line, the patterns as written
# (for the self-test's canaries) and their line numbers in the words file
# (same order), for the report. A word starts where no letter or digit comes
# before it, or where a lowercase letter meets an uppercase one; it ends the
# same way. Case is tested with /i switched off, because PCRE (git grep) and
# Perl disagree on what \p{Lu} means under /i.
cat > "$work/compile.pl" <<'PERL'
open(N, ">", $ENV{NUMS}) or die;
open(R, ">:encoding(UTF-8)", $ENV{RAW}) or die;
my $start = '(?:(?<![\p{L}\p{N}])|(?-i:(?<=\p{Ll})(?=\p{Lu})))';
my $end = '(?:(?![\p{L}\p{N}])|(?-i:(?<=\p{Ll})(?=\p{Lu})))';
while (<STDIN>) {
  chomp; s/\r$//;
  next if /^\s*(#|$)/;
  my $p = $_;
  $p = "$start$p$end" if $p =~ /^\p{L}+$/;
  eval { qr/$p/i; 1 } or do { print STDERR "leak-gate: the pattern on line $. does not compile\n"; exit 3 };
  print "$p\n"; print R "$_\n"; print N "$.\n";
}
PERL
NUMS="$work/nums" RAW="$work/raw" perl -CSD "$work/compile.pl" < "$words" > "$work/patterns" || die "the words file does not compile"
[ -s "$work/patterns" ] || die "the words file holds no pattern"

# Reads `<where>\t<text>` lines; prints where each hit is and the patterns
# that matched, named by their line in the words file, never the text.
cat > "$work/mask.pl" <<'PERL'
open(P, "<:encoding(UTF-8)", $ENV{PATS}) or die; chomp(my @p = <P>);
open(N, "<", $ENV{NUMS}) or die; chomp(my @nums = <N>);
my @pats = map { qr/$_/i } @p;
while (<STDIN>) {
  chomp; my ($where, $text) = split /\t/, $_, 2; $text //= "";
  my @hit = map { $nums[$_] } grep { $text =~ $pats[$_] } 0 .. $#pats;
  next if $ENV{ONLY_HITS} && !@hit;
  printf "%s: [pattern %s]\n", $where, (@hit ? join(",", @hit) : "?");
}
PERL

cd "$top"
export PATS="$work/patterns" NUMS="$work/nums" RAW="$work/raw"

if [ "$self_test" = 1 ]; then
  export EXPECT="$work/expect"
  # One canary per pattern: a string the pattern matches, derived from it.
  perl -CSD -e '
    open(P, "<:encoding(UTF-8)", $ENV{PATS}) or die; chomp(my @p = <P>);
    open(R, "<:encoding(UTF-8)", $ENV{RAW}) or die; chomp(my @raw = <R>);
    open(N, "<", $ENV{NUMS}) or die; chomp(my @nums = <N>);
    open(E, ">", $ENV{EXPECT}) or die;
    my $none = 0;
    for my $i (0 .. $#p) {
      my $p = $p[$i];
      my $s = $raw[$i];
      $s =~ s/\(\?<?[!=](?:[^()]|\([^()]*\))*\)//g;  # lookarounds
      $s =~ s/\\b//g;
      $s =~ s/\\s[+*?]?/ /g;
      $s =~ s/\[\\?(.)[^\]]*\]/$1/g;                # a class: its first member
      $s =~ s/(?<!\\)[?*]//g;                       # optional parts: kept once
      $s =~ s/(?<!\\)\+//g;
      $s =~ s/\\(.)/$1/g;
      my $line = "canary $s canary";
      if ($line =~ /$p/i) { print "$line\n"; print E "$nums[$i]\n" } else { $none++ }
    }
    print STDERR "$none\n";
  ' > "$work/canaries" 2> "$work/underived"
  planted=$(wc -l < "$work/canaries" | tr -d " ")
  underived=$(cat "$work/underived")
  [ "$planted" -gt 0 ] || die "self-test: no pattern yields a canary"
  git -c core.hooksPath=/dev/null clone -q "$top" "$work/clone" || die "self-test: cannot clone"
  cp "$work/canaries" "$work/clone/leak-gate-canary.txt"
  (
    cd "$work/clone"
    git add leak-gate-canary.txt
    git -c core.hooksPath=/dev/null -c user.name=leak-gate -c user.email=leak-gate@example.invalid -c commit.gpgsign=false \
      commit -q --no-verify -F leak-gate-canary.txt
  ) || die "self-test: cannot commit the canary"
  set +e
  (cd "$work/clone" && "$self" "$words" HEAD~1..HEAD) > "$work/report" 2>&1
  status=$?
  set -e
  # A canary counts only when the report names its own pattern on its line.
  caught() {
    perl -ne '
      BEGIN { open(E, "<", $ENV{EXPECT}) or die; chomp(@e = <E>) }
      next unless /^\Q$ENV{PREFIX}\E[^:]*:(\d+): \[pattern ([\d,]+)\]/;
      my ($n, $hit) = ($1, $2);
      $c++ if defined $e[$n - 1] && grep { $_ eq $e[$n - 1] } split /,/, $hit;
      END { print $c + 0, "\n" }
    ' "$work/report"
  }
  in_file=$(PREFIX=leak-gate-canary.txt caught)
  in_msg=$(PREFIX="commit " caught)
  echo "self-test: the gate exited $status; caught $in_file of $planted canaries in a tracked file and $in_msg of $planted in a commit message ($underived patterns yield no canary)"
  if [ "$status" = 1 ] && [ "$in_file" = "$planted" ] && [ "$in_msg" = "$planted" ]; then
    echo "self-test: pass, the gate fails on a planted word"
    exit 0
  fi
  echo "self-test: FAIL, the gate let a planted word through"
  exit 1
fi

# Tracked files.
set +e
git grep -P -i -n -I --no-color -f "$work/patterns" > "$work/grep"
status=$?
set -e
[ "$status" -le 1 ] || die "git grep failed (exit $status); this git may lack PCRE support"
perl -CSD -pe 's/^([^:]*:\d+):/$1\t/' "$work/grep" | perl -CSD "$work/mask.pl" > "$work/tree"

# Commit messages.
git rev-parse -q --verify "${range##*..}^{commit}" > /dev/null || die "no such revision: $range"
git log --format='%x00%h%n%B' "$range" -- | perl -CSD -ne '
  if (s/^\0//) { chomp; $sha = $_; $n = 0; next }
  $n++; print "commit $sha:$n\t$_";
' | ONLY_HITS=1 perl -CSD "$work/mask.pl" > "$work/log"

cat "$work/tree" "$work/log"
files=$(wc -l < "$work/tree" | tr -d " ")
msgs=$(wc -l < "$work/log" | tr -d " ")
echo "leak gate: $files hits in tracked files, $msgs in commit messages ($range)"
[ "$files" = 0 ] && [ "$msgs" = 0 ]
