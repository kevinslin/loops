- [ ] $needs_input is not a custom isgnal
- [ ] signals need to be cleaned up. currently 

- [x] each codex starts a fresh session in inner loop. should re-use existing session by resuming the previous session instead. should use best practices mentioned in https://x.com/trq212/status/2024574133011673516?s=20 (please create research doc on prompt caching based on this article first)

- [ ] bug: inner loop appears "stuck" in `PR_APPROVED` when PR is approved but not merged. cleanup runs once, then loop polls forever waiting for `mergedAt` without escalating to `NEEDS_INPUT` or marking done. reproduce with `review_status=approved` and `mergedAt=null`; expected behavior: either finish after cleanup (if merge is out of scope) or escalate with clear handoff after a bounded wait.
