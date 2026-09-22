# OpenCode Transient Stream Error Did Not Trigger Model Fallback

## Problem

Repair session `2c455e516c9b4eaaa3ff9731b363ac68` for report
`53e47af5eb9047d99cc6113268902788` failed while generating a target-specific
test for `pipecat/sop/nlu.py`.

The OpenCode session started and read target context, but produced no patch. It
then terminated with:

```text
stream error: stream ID 1; INTERNAL_ERROR; received from peer
```

UTA marked the target `PROVIDER_ERROR` instead of falling back to the next
configured provider/model.

## Root Cause

`classify_provider_model_error()` recognized authentication, rate-limit,
missing-model, disabled-model, and unavailable-model errors. It did not
recognize transient HTTP/2 stream and connection failures. Consequently the
turn result had `fallback_eligible=false`, so the workflow converted the
provider transport failure into a terminal repair failure.

The raw OpenCode JSONL proves that a session was created successfully and the
error originated from `token-pool/gpt-5.6-terra`. A nearby
`token-pool/gpt-5.5` attempt encountered the same transport signature.

## Chosen Fix

Treat narrowly recognized transient provider transport signatures as
`provider_transport_error`, making them eligible for the existing bounded
provider/model fallback flow. Generic agent and command errors remain terminal.

## Verification

- Reproduce the exact structured OpenCode error in a unit test.
- Verify the result is fallback-eligible with reason
  `provider_transport_error`.
- Verify the existing generic-error non-fallback regression remains green.

