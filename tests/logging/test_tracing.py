# Copyright 2022 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer
from twisted.test.proto_helpers import MemoryReactorClock

from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.logging.tracing import start_active_span, tag_args, trace_with_opname
from synapse.util import Clock

from tests.unittest import TestCase

try:
    import opentelemetry
    import opentelemetry.sdk.trace
    import opentelemetry.sdk.trace.export
    import opentelemetry.sdk.trace.export.in_memory_span_exporter
    import opentelemetry.trace
    import opentelemetry.trace.propagation
except ImportError:
    opentelemetry = None  # type: ignore[assignment]


class TracingTestCase(TestCase):
    """
    Test logging contexts and active opentelemetry spans.
    """

    if opentelemetry is None:
        skip = "Requires opentelemetry"  # type: ignore[unreachable]

    def setUp(self) -> None:
        # since this is a unit test, we don't really want to mess around with the
        # global variables that power opentelemetry. We create our own tracer instance
        # and test with it.

        self._tracer_provider = opentelemetry.sdk.trace.TracerProvider()

        self._exporter = (
            opentelemetry.sdk.trace.export.in_memory_span_exporter.InMemorySpanExporter()
        )
        processor = opentelemetry.sdk.trace.export.SimpleSpanProcessor(self._exporter)
        self._tracer_provider.add_span_processor(processor)

        self._tracer = self._tracer_provider.get_tracer(__name__)

    def test_start_active_span(self) -> None:
        # This means no current span
        self.assertEqual(
            opentelemetry.trace.get_current_span(), opentelemetry.trace.INVALID_SPAN
        )

        # start_active_span should start and activate a span.
        with start_active_span("new-span", tracer=self._tracer) as span:
            self.assertEqual(opentelemetry.trace.get_current_span(), span)

        # ... but leaving it unsets the active span, and finishes the span.
        self.assertEqual(
            opentelemetry.trace.get_current_span(), opentelemetry.trace.INVALID_SPAN
        )

        # the span should have been reported
        self.assertListEqual(
            [span.name for span in self._exporter.get_finished_spans()], ["new-span"]
        )

    def test_nested_spans(self) -> None:
        """Starting two spans off inside each other should work"""
        with start_active_span("root_span", tracer=self._tracer) as root_span:
            self.assertEqual(opentelemetry.trace.get_current_span(), root_span)

            with start_active_span(
                "child_span1",
                tracer=self._tracer,
            ) as child_span1:
                self.assertEqual(
                    opentelemetry.trace.get_current_span(),
                    child_span1,
                    "child_span1 was not activated",
                )

                with start_active_span(
                    "child_span2",
                    tracer=self._tracer,
                ) as child_span2:
                    self.assertEqual(
                        opentelemetry.trace.get_current_span(), child_span2
                    )

            # the root scope should be restored
            self.assertEqual(opentelemetry.trace.get_current_span(), root_span)

        # Active span is unset now that we're outside of the `with` scopes
        self.assertEqual(
            opentelemetry.trace.get_current_span(), opentelemetry.trace.INVALID_SPAN
        )

        # the spans should be reported in order of their finishing.
        self.assertListEqual(
            [span.name for span in self._exporter.get_finished_spans()],
            ["child_span2", "child_span1", "root_span"],
        )

    def test_side_by_side_spans(self) -> None:
        with start_active_span("span1", tracer=self._tracer), start_active_span(
            "span2", tracer=self._tracer
        ) as span2:
            # We expect the last span in `with` list to be active
            self.assertEqual(opentelemetry.trace.get_current_span(), span2)

        # Active span is unset now that we're outside of the `with` scopes
        self.assertEqual(
            opentelemetry.trace.get_current_span(), opentelemetry.trace.INVALID_SPAN
        )

        # the spans should be reported in order of their finishing.
        self.assertListEqual(
            [span.name for span in self._exporter.get_finished_spans()],
            ["span2", "span1"],
        )

    def test_overlapping_spans(self) -> None:
        """Overlapping spans which are not neatly nested should work"""
        reactor = MemoryReactorClock()
        clock = Clock(reactor)

        async def task(i: int):
            with start_active_span(
                f"task{i}",
                tracer=self._tracer,
            ) as span:
                self.assertEqual(opentelemetry.trace.get_current_span(), span)
                await clock.sleep(4)
                self.assertEqual(opentelemetry.trace.get_current_span(), span)

        async def root():
            with start_active_span("root_span", tracer=self._tracer) as root_span:
                self.assertEqual(opentelemetry.trace.get_current_span(), root_span)

                d1 = run_in_background(task, 1)
                await clock.sleep(2)
                d2 = run_in_background(task, 2)

                # because we did run_in_background, the active span should still be the
                # root.
                self.assertEqual(opentelemetry.trace.get_current_span(), root_span)

                await make_deferred_yieldable(
                    defer.gatherResults([d1, d2], consumeErrors=True)
                )

                self.assertEqual(opentelemetry.trace.get_current_span(), root_span)

        # start the test off
        root_defferred = defer.ensureDeferred(root())

        # let the tasks complete
        reactor.pump((2,) * 8)

        self.successResultOf(root_defferred)
        # Active span is unset now that we're outside of the `with` scopes
        self.assertEqual(
            opentelemetry.trace.get_current_span(), opentelemetry.trace.INVALID_SPAN
        )

        # the spans should be reported in order of their finishing: task 1, task 2,
        # root.
        self.assertListEqual(
            [span.name for span in self._exporter.get_finished_spans()],
            ["task1", "task2", "root_span"],
        )

    def test_trace_decorator_sync(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with sync functions
        """

        @trace_with_opname("fixture_sync_func", tracer=self._tracer)
        @tag_args
        def fixture_sync_func() -> str:
            return "foo"

        result = fixture_sync_func()
        self.assertEqual(result, "foo")

        # the span should have been reported
        self.assertEqual(
            [span.name for span in self._exporter.get_finished_spans()],
            ["fixture_sync_func"],
        )

    def test_trace_decorator_deferred(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with functions that return deferreds
        """
        reactor = MemoryReactorClock()

        @trace_with_opname("fixture_deferred_func", tracer=self._tracer)
        @tag_args
        def fixture_deferred_func() -> "defer.Deferred[str]":
            d1: defer.Deferred[str] = defer.Deferred()
            d1.callback("foo")
            return d1

        result_d1 = fixture_deferred_func()

        # let the tasks complete
        reactor.pump((2,) * 8)

        self.assertEqual(self.successResultOf(result_d1), "foo")

        # the span should have been reported
        self.assertEqual(
            [span.name for span in self._exporter.get_finished_spans()],
            ["fixture_deferred_func"],
        )

    def test_trace_decorator_async(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with async functions
        """
        reactor = MemoryReactorClock()

        @trace_with_opname("fixture_async_func", tracer=self._tracer)
        @tag_args
        async def fixture_async_func() -> str:
            return "foo"

        d1 = defer.ensureDeferred(fixture_async_func())

        # let the tasks complete
        reactor.pump((2,) * 8)

        self.assertEqual(self.successResultOf(d1), "foo")

        # the span should have been reported
        self.assertEqual(
            [span.name for span in self._exporter.get_finished_spans()],
            ["fixture_async_func"],
        )
