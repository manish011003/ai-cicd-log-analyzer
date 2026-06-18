"""Detector — Go runtime panics + goroutine backtraces.

Go's frame layout is two lines per frame::

    main.processPayment(0x0)
            /app/internal/payment/handler.go:42 +0xa5

We match the **second** line (the deep-link target carries the
``file:line``) and recover the function name from the previous non-blank
line via :data:`StackLanguage.function_on_previous_line`. Bug site is the
topmost project frame, matching the Java/Node convention (Go prints the
panicking goroutine's deepest call first).

Framework filter excludes anything under ``GOROOT``/``$GOPATH`` module
caches, the Go runtime's internal packages, and the ``/usr/local/go``
install layout most container images use.
"""

from __future__ import annotations

from . import register
from .generic_stack import GenericStackDetector, StackLanguage


_GO = StackLanguage(
    name="go_panic",
    priority=50,
    description="Localizes Go panics to the topmost project frame in the goroutine trace.",
    # Probe — any indented "...go:NNN +0xHEX" line is a strong Go signal.
    # The hex PC offset is optional because some tools strip it.
    probe_pattern=r"^\s+\S+\.go:\d+(?:\s+\+0x[0-9a-f]+)?\s*$",
    # Frame — the file:line line itself. No function group here; we look up
    # the line above (see function_on_previous_line below).
    frame_pattern=r"^\s+(?P<file>\S+\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-f]+)?\s*$",
    # Loose Go-stack pattern so the function-name line that sits between
    # every two ``file.go:NN`` frames doesn't split each frame into its
    # own 1-frame run. Matches:
    #   * a function-call line   ``main.processPayment(0x0)``
    #   * a method-receiver line ``(*Server).serve(...)``
    #   * a goroutine header     ``goroutine 1 [running]:``
    # All three are stack-trace decoration we want to keep together.
    run_separator_pattern=(
        r"^(?:\(\*?[\w.]+\)|[\w./]+)\.[\w$]+\([^)]*\)\s*$"  # function/method call
        r"|^goroutine\s+\d+\s+\["                            # goroutine header
        r"|^\s+\S+\.go:\d+"                                  # file:line (already a frame)
        r"|^created\s+by\s+\S+\.\S+"                         # "created by main.f"
    ),
    framework_paths=(
        "/usr/local/go/src/",
        "/usr/lib/go/src/",
        "/go/pkg/mod/",
        "/go/src/runtime/",
        "runtime/proc.go",
        "runtime/asm_",
        "runtime/panic.go",
        "runtime/signal_",
        "runtime/sigqueue.go",
    ),
    bug_site="top",
    function_on_previous_line=True,
    # Go panics usually start with a "panic:" line above the goroutine block.
    # We treat "panic" as the synthetic class and capture the message.
    cause_pattern=r"^(?P<cls>panic|fatal error|runtime error):\s*(?P<msg>.*)$",
    cause_position="above",
    cause_lookback=12,  # generous: there's often a "[signal …]" line in between
)


register(GenericStackDetector(_GO))
