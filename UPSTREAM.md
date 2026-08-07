# Upstream pin

8008TUB3 is a downstream distribution of **FieldStation42** by Shane Mason (MPL-2.0).

    repo    https://github.com/shane-mason/FieldStation42
    commit  2baa022d26197d56fe80a7e656340770a4ff9638
    dated   2026-08-05

## Why this file exists

FieldStation42 has **no tags and no releases** — as of 2026-08-07 the repository has ~727
commits and zero published versions, and 91% of them are by a single author. There is no
version string to depend on, so the only honest way to have a reproducible build is to pin a
commit SHA and run our own acceptance tests against it.

Bumping this pin is a deliberate act with a test pass attached, never an incidental `git pull`.

The checkout itself lives at `vendor/FieldStation42` and is gitignored. To reproduce:

    git clone https://github.com/shane-mason/FieldStation42.git vendor/FieldStation42
    git -C vendor/FieldStation42 checkout 2baa022d26197d56fe80a7e656340770a4ff9638

## Licence boundary

MPL-2.0 is **file-level** copyleft. Any FieldStation42 file we modify stays MPL-2.0 and its
source must be offered; anything we author ourselves is ours to license. The practical rule
that follows: **prefer extension points over edits.** A change made by passing an argument
costs nothing, while the same change made by editing an upstream file creates a file we must
carry, re-merge, and publish forever.

## Being a good downstream

Shane Mason funds this work through Patreon and there is already a third party selling
FieldStation42 hardware. Before 8008TUB3 is announced publicly: open a conversation with him,
credit prominently, and upstream every fix that isn't specific to our appliance.
