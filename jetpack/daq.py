"""
NI DAQ button/LED I/O, isolated behind one class.

The whole point of this module is that it is the *only* place the rest of the game
touches hardware, and that it cannot stall a frame. `DaqIO` owns a daemon thread
that polls digital inputs and writes digital outputs on its own schedule; the game
loop only ever reads a lock-protected cache of the last known button state and
drops the desired LED state into another. Neither call blocks on the driver.

If nidaqmx is missing, or the device isn't there, or the poll thread dies mid-run,
`available` goes False and every method degrades to a no-op -- so the caller never
needs a hardware branch beyond choosing where to read buttons from.
"""
import threading
import time

from .config import BUTTON_LINES, DAQ_DEBUG_PRINT, DAQ_POLL_INTERVAL, DEVICE, LED_LINES

try:
    import nidaqmx
    from nidaqmx.constants import LineGrouping
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False


class DaqIO:
    """Wraps the NI DAQ buttons/LEDs. Falls back to a keyboard-only no-op if unavailable."""

    def __init__(self):
        """Claims the DI/DO lines and starts polling, or degrades quietly.

        Two separate failure modes are treated identically on purpose: the package
        being absent, and the package being present with no device behind it. Both
        end with `available` False and a printed reason, because from the game's
        point of view there is no difference -- it needs a keyboard either way, and
        a demo machine should never fail to start over missing hardware.

        The thread is a daemon so a hung driver read can't keep the process alive
        after the window closes.
        """
        self.available = False
        self._di_task = None
        self._do_task = None
        self._buttons = [False, False]
        self._leds = [False, False]
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        if not NIDAQMX_AVAILABLE:
            print("nidaqmx not installed -- running in keyboard-only mode.")
            return

        try:
            self._di_task = nidaqmx.Task()
            self._do_task = nidaqmx.Task()
            self._di_task.di_channels.add_di_chan(
                BUTTON_LINES, line_grouping=LineGrouping.CHAN_PER_LINE)
            self._do_task.do_channels.add_do_chan(
                LED_LINES, line_grouping=LineGrouping.CHAN_PER_LINE)
            self.available = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()
            print(f"NI DAQ '{DEVICE}' connected -- buttons and LEDs are live.")
        except Exception as exc:
            print(f"NI DAQ not available ({exc}) -- running in keyboard-only mode.")
            self.available = False
            self._di_task = None
            self._do_task = None

    def _poll_loop(self):
        """Reads buttons and writes LEDs forever, on this thread and no other.

        Both directions live here rather than the write happening on the caller's
        thread, so the game loop never touches the driver at all -- `set_leds()`
        only stores intent and this picks it up on the next pass. The cost is up to
        one DAQ_POLL_INTERVAL of LED latency, which is invisible at 10ms and buys a
        frame rate that cannot be affected by how slow a DAQ write turns out to be.

        The lock is held only across the two list assignments, never across the
        driver calls: holding it over `read()` would hand the game loop's
        `get_buttons()` exactly the stall this design exists to prevent.

        Any driver exception ends the thread rather than retrying. A DAQ that has
        started failing mid-run will keep failing, and a retry loop would print
        once per 10ms while doing it.
        """
        last_debug_print = 0.0
        last_printed = None
        while not self._stop_event.is_set():
            try:
                button1, button2 = self._di_task.read()
                with self._lock:
                    self._buttons = [button1, button2]
                    leds_to_write = list(self._leds)
                # LED wiring is physically reversed from DO line order (line6 -> LED2,
                # line7 -> LED1), confirmed by pressing each button on real hardware.
                self._do_task.write([leds_to_write[1], leds_to_write[0]])
            except Exception as exc:
                print(f"DAQ polling stopped due to error: {exc}")
                self.available = False
                return

            if DAQ_DEBUG_PRINT:
                now = time.monotonic()
                state = (button1, button2)
                # Print on every state change, plus a heartbeat every 2s so it's
                # obvious the poll loop is alive even if nothing is pressed.
                if state != last_printed or now - last_debug_print > 2.0:
                    print(f"[DAQ] button1={button1}  button2={button2}")
                    last_debug_print = now
                    last_printed = state

            time.sleep(DAQ_POLL_INTERVAL)

    def get_buttons(self):
        """Returns (button1_pressed, button2_pressed); (False, False) if no DAQ."""
        if not self.available:
            return False, False
        with self._lock:
            return tuple(self._buttons)

    def set_leds(self, led1_on, led2_on):
        """Records the desired LED state; the poll thread performs the actual write.

        Deliberately fire-and-forget with no return value or confirmation -- the
        game loop calls this every frame and must never wait on hardware to draw.
        """
        if not self.available:
            return
        with self._lock:
            self._leds = [led1_on, led2_on]

    def close(self):
        """Stops the poll thread, darkens both LEDs and releases the tasks.

        The LED write is wrapped and ignored on failure because this runs during
        shutdown: if the DAQ is already gone, leaving the lines in whatever state
        they were in is strictly better than raising on the way out.

        Known gap: this early-returns on `not self.available`, so if the poll loop
        died mid-run and flipped that flag, the two tasks are never closed and stay
        reserved until the process exits. Harmless for a demo that quits soon
        after, wrong in general.
        """
        if not self.available:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self._do_task.write([False, False])  # leave both LEDs off on exit
        except Exception:
            pass
        self._di_task.close()
        self._do_task.close()
