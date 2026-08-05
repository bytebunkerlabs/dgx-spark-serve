# h3-fp8-0.26 — MiniMax-H3 online FP8, backported onto the v0.26.0 image

Upstream merged H3-aware FP8 (vllm-project/vllm-omni#5737, commit
`b9a51592f0`, Apache-2.0) 27 hours AFTER the v0.26.0 release was cut (08-03 08:26 -> 08-04 11:35 UTC), so
no published image has it — and without FP8 the DiT alone pushes the weights
past one Spark's 128 GB. The day-0 `minimax-h3` tag crashes in this exact
path (its dev-snapshot omni calls H3's 2-arg fc1 weight loader with 3 args
through vLLM's online-quant layerwise reloader).

This mod is the two runtime files from the merge commit, verbatim:

    overlay/.../minimax_h3_transformer.py   (+104/-52 vs release: loader rework)
    overlay/.../pipeline_minimax_h3.py      (+11/-1)

Plus one GB10 pacing fix (2026-08-05), ours not upstream's:

    overlay/.../diffusion_engine.py         (release file, one constant changed:
                                             _ASYNC_OUTPUT_TIMEOUT 30 -> 600)

The stock 30 s inter-output timeout assumes datacenter step times. A GB10
runs large shapes at 38+ s/step with a minutes-long VAE tail — the client
gave up on a render whose denoise had COMPLETED, and the resulting abort
crashed vllm-omni's DiffusionResultPump thread (InvalidStateError: CANCELLED,
reproduced twice), leaving a zombie engine. Until upstream makes the timeout
configurable and survives aborts, both problems are dodged here.

Verified before vendoring: the PR adds no new imports, and git history shows
no other post-release commit touches either file — the release tag IS the
commit directly under the merge. `launch-solo.sh` bind-mounts everything
under `overlay/` read-only over the container's filesystem.

Delete this mod when the first vllm-omni release after 2026-08-04 ships.
