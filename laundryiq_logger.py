"""
LaundryIQ Data Logger - BLE current sensor data logging application

Logs current sensor data (CT clamp, Hall1, Hall2) from LaundryIQ BLE device,
displays real-time plots, tracks machine states, and exports to CSV.

Requirements:
  pip install -r requirements.txt

Usage:
  python laundryiq_logger.py
"""

from __future__ import annotations

import asyncio
import csv
import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from bleak import BleakClient, BleakScanner


# Nordic UART Service (NUS) UUIDs
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


@dataclass
class FoundDevice:
    name: str
    address: str
    rssi: Optional[int]


@dataclass
class DataSample:
    timestamp: float
    ct_clamp: Optional[float]
    hall1: Optional[float]
    hall2: Optional[float]
    state: str
    elapsed_time: float


@dataclass
class StateChange:
    timestamp: float
    state: str
    elapsed_time: float


class BleWorker(threading.Thread):
    """Runs Bleak on an asyncio loop in a background thread."""

    def __init__(self, events: queue.Queue[tuple[str, object]]):
        super().__init__(daemon=True)
        self.events = events
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional[BleakClient] = None
        self._connected_address: Optional[str] = None

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        if self._loop is None:
            raise RuntimeError("BLE worker not started yet.")
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def scan(self, name_filter: str, timeout_s: float) -> None:
        self._submit(self._scan_and_post(name_filter, timeout_s))

    def connect(self, address: str) -> None:
        self._submit(self._connect_and_post(address))

    def disconnect(self) -> None:
        self._submit(self._disconnect_and_post())

    def shutdown(self) -> None:
        if self._loop is None:
            return
        self._submit(self._disconnect_and_post())
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _scan_and_post(self, name_filter: str, timeout_s: float) -> None:
        try:
            devices = await BleakScanner.discover(timeout=timeout_s)
            found: list[FoundDevice] = []
            for d in devices:
                name = (getattr(d, "name", "") or "").strip()
                if name_filter and name_filter.lower() not in name.lower():
                    continue
                found.append(
                    FoundDevice(
                        name=name or "<unknown>",
                        address=str(getattr(d, "address", "")),
                        rssi=getattr(d, "rssi", None),
                    )
                )
            found.sort(key=lambda x: (x.name == "<unknown>", -(x.rssi or -999)))
            self.events.put(("scan_result", found))
        except Exception as e:
            self.events.put(("error", f"Scan failed: {e!r}"))

    async def _connect_and_post(self, address: str) -> None:
        try:
            await self._disconnect_silent()

            client = BleakClient(address)

            def on_disconnect(_: BleakClient):
                self.events.put(("status", "Disconnected"))
                self.events.put(("connected", False))

            set_cb = getattr(client, "set_disconnected_callback", None)
            if callable(set_cb):
                set_cb(on_disconnect)

            ok = await client.connect()
            if not ok and not getattr(client, "is_connected", False):
                raise RuntimeError("connect() returned false")

            services = getattr(client, "services", None)
            if services is None:
                get_services = getattr(client, "get_services", None)
                if callable(get_services):
                    try:
                        services = await get_services()
                    except TypeError:
                        services = get_services()
            if services is not None and hasattr(services, "get_service"):
                if services.get_service(NUS_SERVICE_UUID) is None:
                    raise RuntimeError(f"Device missing NUS service {NUS_SERVICE_UUID}")

            def on_tx(_: int, data: bytearray):
                self.events.put(("rx", bytes(data)))

            try:
                await client.start_notify(NUS_TX_CHAR_UUID, on_tx)
            except Exception as e:
                raise RuntimeError(
                    "Couldn't subscribe to NUS TX. Wrong device, or firmware not running NUS."
                ) from e

            self._client = client
            self._connected_address = address
            self.events.put(("status", f"Connected to {address}"))
            self.events.put(("connected", True))
        except Exception as e:
            self.events.put(("error", f"Connect failed: {e}"))
            await self._disconnect_silent()
            self.events.put(("connected", False))

    async def _disconnect_and_post(self) -> None:
        await self._disconnect_silent()
        self.events.put(("status", "Disconnected"))
        self.events.put(("connected", False))

    async def _disconnect_silent(self) -> None:
        if self._client is None:
            return
        try:
            try:
                await self._client.stop_notify(NUS_TX_CHAR_UUID)
            except Exception:
                pass
            await self._client.disconnect()
        except Exception:
            pass
        finally:
            self._client = None
            self._connected_address = None


class DataParser:
    """Parses incoming BLE data and extracts sensor values."""

    # Patterns: CT_A:1.234 or CT_A = 1.234, HALL1_A:2.345, HALL2_A:3.456
    PATTERNS = {
        "ct_clamp": re.compile(r"CT_A[:\s=]+([\d.+-]+)"),
        "hall1": re.compile(r"HALL1_A[:\s=]+([\d.+-]+)"),
        "hall2": re.compile(r"HALL2_A[:\s=]+([\d.+-]+)"),
    }

    def __init__(self):
        self._buffer = ""

    def parse(self, data: bytes) -> dict[str, Optional[float]]:
        """Parse data and return dict with ct_clamp, hall1, hall2 values."""
        self._buffer += data.decode("utf-8", errors="replace")
        result = {"ct_clamp": None, "hall1": None, "hall2": None}

        # Process complete lines
        lines = self._buffer.split("\n")
        self._buffer = lines[-1]  # Keep incomplete line in buffer

        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue

            for key, pattern in self.PATTERNS.items():
                match = pattern.search(line)
                if match:
                    try:
                        result[key] = float(match.group(1))
                    except ValueError:
                        pass

        return result


class LaundryIQLogger(ttk.Frame):
    """Main application window."""

    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.master = master
        self.master.title("LaundryIQ Data Logger")
        self.master.minsize(900, 600)

        # BLE worker
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker = BleWorker(self.events)
        self.worker.start()

        # Data management
        self.parser = DataParser()
        self.data_buffer: deque[DataSample] = deque(maxlen=10000)
        self.state_changes: list[StateChange] = []
        self.current_state = "Off"
        self.logging_start_time: Optional[float] = None
        self.is_logging = False
        self.csv_file: Optional[csv.writer] = None
        self.csv_file_handle = None

        # Sampling control
        self.sample_interval = 0.1  # 10Hz default
        self.last_sample_time = 0.0
        self.pending_data = {"ct_clamp": None, "hall1": None, "hall2": None}
        
        # Calibration factors (multipliers to adjust readings)
        self.calibration_factors = {
            "ct_clamp": 1.0,  # Default: no adjustment
            "hall1": 1.0,
            "hall2": 1.0,
        }

        # Machine settings
        self.machine_type = tk.StringVar(value="Washer")
        self.washer_settings = {
            "cycle": tk.StringVar(value="Normal"),
            "temp": tk.StringVar(value="Warm"),
            "spin": tk.StringVar(value="Medium"),
        }
        self.dryer_settings = {
            "dry_level": tk.StringVar(value="Normal Dry"),
            "temp": tk.StringVar(value="Medium"),
            "time": tk.StringVar(value="40min"),
        }

        # Sensor visibility
        self.sensor_vars = {
            "ct_clamp": tk.BooleanVar(value=True),
            "hall1": tk.BooleanVar(value=True),
            "hall2": tk.BooleanVar(value=True),
        }

        # UI variables
        self.name_var = tk.StringVar(value="LIQ")
        self.timeout_var = tk.DoubleVar(value=2.0)
        self.status_var = tk.StringVar(value="Idle")
        self.connected_var = tk.BooleanVar(value=False)
        self.filename_var = tk.StringVar()
        self.default_downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
        self.path_var = tk.StringVar(value=self.default_downloads_path)
        self.current_state_var = tk.StringVar(value="Off")
        
        # Calibration UI variables
        self.ct_calib_var = tk.StringVar(value="1.0")
        self.hall1_calib_var = tk.StringVar(value="1.0")
        self.hall2_calib_var = tk.StringVar(value="1.0")
        self.mm_reading_var = tk.StringVar()
        self.clamp_reading_var = tk.StringVar()
        self.hall1_mm_var = tk.StringVar()
        self.hall1_sensor_var = tk.StringVar()
        self.hall2_mm_var = tk.StringVar()
        self.hall2_sensor_var = tk.StringVar()

        self._devices: list[FoundDevice] = []

        self._build_ui()
        self._poll_events()
        self._update_plot()

    def _build_ui(self) -> None:
        """Build the user interface."""
        # Main container
        main_container = ttk.Frame(self)
        main_container.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Left panel with scrollbar
        left_container = ttk.Frame(main_container, width=300)
        left_container.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 4))
        left_container.pack_propagate(False)

        # Create canvas and scrollbar for left panel
        canvas = tk.Canvas(left_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(left_container, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw", width=300)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Update scroll region when content changes
        def _configure_scroll_region(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        scrollable_frame.bind("<Configure>", _configure_scroll_region)
        
        # Bind mousewheel to canvas (Windows)
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _on_mousewheel)

        # Right panel
        right_panel = ttk.Frame(main_container)
        right_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_left_panel(scrollable_frame)
        self._build_right_panel(right_panel)

        self.pack(fill=tk.BOTH, expand=True)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        """Build left control panel."""
        # BLE Connection Section - more compact
        conn_frame = ttk.LabelFrame(parent, text="BLE", padding=3)
        conn_frame.pack(fill=tk.X, pady=(0, 3))

        # Top row: Name and Scan time side by side
        ttk.Label(conn_frame, text="Name:", font=("TkDefaultFont", 7)).grid(row=0, column=0, sticky="w", pady=0)
        ttk.Entry(conn_frame, textvariable=self.name_var, width=10, font=("TkDefaultFont", 7)).grid(
            row=0, column=1, sticky="ew", padx=(2, 4), pady=0
        )
        ttk.Label(conn_frame, text="(s):", font=("TkDefaultFont", 7)).grid(row=0, column=2, sticky="w", pady=0)
        ttk.Spinbox(
            conn_frame, from_=1, to=30, increment=1, textvariable=self.timeout_var, width=4, font=("TkDefaultFont", 7)
        ).grid(row=0, column=3, sticky="w", padx=(2, 0), pady=0)

        # Buttons row
        btn_frame = ttk.Frame(conn_frame)
        btn_frame.grid(row=1, column=0, columnspan=4, pady=(2, 0))
        ttk.Button(btn_frame, text="Scan", command=self._on_scan, width=7).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(btn_frame, text="Conn", command=self._on_connect, width=7).pack(side=tk.LEFT, padx=(0, 2))
        disconnect_btn = ttk.Button(btn_frame, text="Disc", command=self._on_disconnect, state=tk.DISABLED, width=7)
        disconnect_btn.pack(side=tk.LEFT)
        self.disconnect_btn = disconnect_btn

        self.device_list = tk.Listbox(conn_frame, height=2, font=("TkDefaultFont", 7))
        self.device_list.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(2, 0))

        conn_frame.columnconfigure(1, weight=1)

        # Machine Settings Section - more compact
        settings_frame = ttk.LabelFrame(parent, text="Machine", padding=3)
        settings_frame.pack(fill=tk.X, pady=(0, 3))

        ttk.Label(settings_frame, text="Type:", font=("TkDefaultFont", 7)).grid(row=0, column=0, sticky="w", pady=0)
        machine_combo = ttk.Combobox(
            settings_frame, textvariable=self.machine_type, values=["Washer", "Dryer"], state="readonly", width=10, font=("TkDefaultFont", 7)
        )
        machine_combo.grid(row=0, column=1, sticky="ew", padx=(2, 0), pady=0)
        machine_combo.bind("<<ComboboxSelected>>", lambda e: self._on_machine_type_changed())

        self.settings_container = ttk.Frame(settings_frame)
        self.settings_container.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self._build_settings_dropdowns()

        settings_frame.columnconfigure(1, weight=1)

        # File Location - with path selection
        file_frame = ttk.LabelFrame(parent, text="File", padding=3)
        file_frame.pack(fill=tk.X, pady=(0, 3))
        
        # Path display and select button
        path_frame = ttk.Frame(file_frame)
        path_frame.pack(fill=tk.X, pady=(0, 2))
        
        path_entry = ttk.Entry(path_frame, textvariable=self.path_var, font=("TkDefaultFont", 7), state="readonly")
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        
        ttk.Button(path_frame, text="Select", command=self._on_select_path, width=8).pack(side=tk.RIGHT)
        
        ttk.Label(file_frame, text="Path will be used when logging starts", font=("TkDefaultFont", 6), foreground="gray").pack(anchor="w")

        # Sensors & Settings combined
        sensors_frame = ttk.LabelFrame(parent, text="Sensors & Settings", padding=3)
        sensors_frame.pack(fill=tk.X, pady=(0, 3))
        
        # Sensors checkboxes in one row
        sensor_check_frame = ttk.Frame(sensors_frame)
        sensor_check_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 2))
        ttk.Checkbutton(sensor_check_frame, text="CT", variable=self.sensor_vars["ct_clamp"], width=6).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Checkbutton(sensor_check_frame, text="H1", variable=self.sensor_vars["hall1"], width=6).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Checkbutton(sensor_check_frame, text="H2", variable=self.sensor_vars["hall2"], width=6).pack(side=tk.LEFT)
        
        # Rate
        ttk.Label(sensors_frame, text="Rate:", font=("TkDefaultFont", 7)).grid(row=1, column=0, sticky="w", pady=0)
        rate_combo = ttk.Combobox(
            sensors_frame,
            values=["1", "2", "5", "10"],
            state="readonly",
            width=6,
            font=("TkDefaultFont", 7)
        )
        rate_combo.set("10")
        rate_combo.grid(row=1, column=1, sticky="w", padx=(2, 0), pady=0)
        rate_combo.bind("<<ComboboxSelected>>", lambda e: self._on_rate_changed(rate_combo.get()))
        
        # CT Calibration
        ttk.Label(sensors_frame, text="CT:", font=("TkDefaultFont", 7)).grid(row=2, column=0, sticky="w", pady=(2, 0))
        ct_calib_entry = ttk.Entry(sensors_frame, textvariable=self.ct_calib_var, width=7, font=("TkDefaultFont", 7))
        ct_calib_entry.grid(row=2, column=1, sticky="w", padx=(2, 4), pady=(2, 0))
        ct_calib_entry.bind("<KeyRelease>", lambda e: self._update_calibration())
        
        # CT Calculator
        ct_calc_frame = ttk.Frame(sensors_frame)
        ct_calc_frame.grid(row=2, column=2, columnspan=2, sticky="ew", padx=(0, 0), pady=(2, 0))
        ttk.Label(ct_calc_frame, text="MM:", font=("TkDefaultFont", 6)).pack(side=tk.LEFT)
        mm_entry = ttk.Entry(ct_calc_frame, textvariable=self.mm_reading_var, width=5, font=("TkDefaultFont", 6))
        mm_entry.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(ct_calc_frame, text="Sen:", font=("TkDefaultFont", 6)).pack(side=tk.LEFT)
        clamp_entry = ttk.Entry(ct_calc_frame, textvariable=self.clamp_reading_var, width=5, font=("TkDefaultFont", 6))
        clamp_entry.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Button(ct_calc_frame, text="Calc", command=lambda: self._calculate_calibration("ct_clamp"), width=4).pack(side=tk.LEFT)
        
        # Hall1 Calibration
        ttk.Label(sensors_frame, text="H1:", font=("TkDefaultFont", 7)).grid(row=3, column=0, sticky="w", pady=(2, 0))
        hall1_calib_entry = ttk.Entry(sensors_frame, textvariable=self.hall1_calib_var, width=7, font=("TkDefaultFont", 7))
        hall1_calib_entry.grid(row=3, column=1, sticky="w", padx=(2, 4), pady=(2, 0))
        hall1_calib_entry.bind("<KeyRelease>", lambda e: self._update_calibration())
        
        # Hall1 Calculator
        h1_calc_frame = ttk.Frame(sensors_frame)
        h1_calc_frame.grid(row=3, column=2, columnspan=2, sticky="ew", pady=(2, 0))
        ttk.Label(h1_calc_frame, text="MM:", font=("TkDefaultFont", 6)).pack(side=tk.LEFT)
        h1_mm_entry = ttk.Entry(h1_calc_frame, textvariable=self.hall1_mm_var, width=5, font=("TkDefaultFont", 6))
        h1_mm_entry.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(h1_calc_frame, text="Sen:", font=("TkDefaultFont", 6)).pack(side=tk.LEFT)
        h1_sensor_entry = ttk.Entry(h1_calc_frame, textvariable=self.hall1_sensor_var, width=5, font=("TkDefaultFont", 6))
        h1_sensor_entry.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Button(h1_calc_frame, text="Calc", command=lambda: self._calculate_calibration("hall1"), width=4).pack(side=tk.LEFT)
        
        # Hall2 Calibration
        ttk.Label(sensors_frame, text="H2:", font=("TkDefaultFont", 7)).grid(row=4, column=0, sticky="w", pady=(2, 0))
        hall2_calib_entry = ttk.Entry(sensors_frame, textvariable=self.hall2_calib_var, width=7, font=("TkDefaultFont", 7))
        hall2_calib_entry.grid(row=4, column=1, sticky="w", padx=(2, 4), pady=(2, 0))
        hall2_calib_entry.bind("<KeyRelease>", lambda e: self._update_calibration())
        
        # Hall2 Calculator
        h2_calc_frame = ttk.Frame(sensors_frame)
        h2_calc_frame.grid(row=4, column=2, columnspan=2, sticky="ew", pady=(2, 0))
        ttk.Label(h2_calc_frame, text="MM:", font=("TkDefaultFont", 6)).pack(side=tk.LEFT)
        h2_mm_entry = ttk.Entry(h2_calc_frame, textvariable=self.hall2_mm_var, width=5, font=("TkDefaultFont", 6))
        h2_mm_entry.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(h2_calc_frame, text="Sen:", font=("TkDefaultFont", 6)).pack(side=tk.LEFT)
        h2_sensor_entry = ttk.Entry(h2_calc_frame, textvariable=self.hall2_sensor_var, width=5, font=("TkDefaultFont", 6))
        h2_sensor_entry.pack(side=tk.LEFT, padx=(2, 2))
        ttk.Button(h2_calc_frame, text="Calc", command=lambda: self._calculate_calibration("hall2"), width=4).pack(side=tk.LEFT)
        
        sensors_frame.columnconfigure(3, weight=1)

        # State Buttons Section - more compact grid
        state_frame = ttk.LabelFrame(parent, text="State", padding=3)
        state_frame.pack(fill=tk.X, pady=(0, 3))

        # Current state display box
        state_display_frame = ttk.Frame(state_frame)
        state_display_frame.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 3))
        
        ttk.Label(state_display_frame, text="Current:", font=("TkDefaultFont", 7)).pack(side=tk.LEFT, padx=(0, 4))
        state_display = ttk.Label(
            state_display_frame, 
            textvariable=self.current_state_var, 
            font=("TkDefaultFont", 9, "bold"),
            foreground="blue",
            relief="sunken",
            padding=(8, 4)
        )
        state_display.pack(side=tk.LEFT, fill=tk.X, expand=True)

        states = ["On", "Off", "Wash", "Rinse", "Spin", "End", "Drying", "Cooling"]
        for i, state in enumerate(states):
            btn = ttk.Button(
                state_frame,
                text=state,
                command=lambda s=state: self._on_state_button(s),
                width=8,
            )
            btn.grid(row=(i // 4) + 1, column=i % 4, padx=1, pady=1, sticky="ew")

        for col in range(4):
            state_frame.columnconfigure(col, weight=1)

        # Logging Controls - most important, keep visible
        log_frame = ttk.LabelFrame(parent, text="Logging", padding=3)
        log_frame.pack(fill=tk.X)

        self.start_btn = ttk.Button(log_frame, text="▶ Start", command=self._on_start_logging)
        self.start_btn.pack(fill=tk.X, pady=(0, 2))

        self.stop_btn = ttk.Button(
            log_frame, text="■ Stop", command=self._on_stop_logging, state=tk.DISABLED
        )
        self.stop_btn.pack(fill=tk.X, pady=(0, 2))

        self.export_btn = ttk.Button(
            log_frame, text="💾 Export", command=self._on_export, state=tk.DISABLED
        )
        self.export_btn.pack(fill=tk.X)

    def _build_settings_dropdowns(self) -> None:
        """Build settings dropdowns based on machine type."""
        # Clear existing widgets
        for widget in self.settings_container.winfo_children():
            widget.destroy()

        if self.machine_type.get() == "Washer":
            ttk.Label(self.settings_container, text="Cycle:", font=("TkDefaultFont", 7)).grid(row=0, column=0, sticky="w", pady=0)
            cycle_combo = ttk.Combobox(
                self.settings_container,
                textvariable=self.washer_settings["cycle"],
                values=["Normal", "Delicates", "Heavy Duty", "Quick Wash"],
                state="readonly",
                width=12,
                font=("TkDefaultFont", 7)
            )
            cycle_combo.grid(row=0, column=1, sticky="ew", padx=(2, 0), pady=0)

            ttk.Label(self.settings_container, text="Temp:", font=("TkDefaultFont", 7)).grid(row=1, column=0, sticky="w", pady=0)
            temp_combo = ttk.Combobox(
                self.settings_container,
                textvariable=self.washer_settings["temp"],
                values=["Hot", "Warm", "Cold"],
                state="readonly",
                width=12,
                font=("TkDefaultFont", 7)
            )
            temp_combo.grid(row=1, column=1, sticky="ew", padx=(2, 0), pady=0)

            ttk.Label(self.settings_container, text="Spin:", font=("TkDefaultFont", 7)).grid(row=2, column=0, sticky="w", pady=0)
            spin_combo = ttk.Combobox(
                self.settings_container,
                textvariable=self.washer_settings["spin"],
                values=["High", "Medium", "Low"],
                state="readonly",
                width=12,
                font=("TkDefaultFont", 7)
            )
            spin_combo.grid(row=2, column=1, sticky="ew", padx=(2, 0), pady=0)

            # Remove filename update bindings - filename generated on Start
        else:  # Dryer
            ttk.Label(self.settings_container, text="Dry Level:", font=("TkDefaultFont", 7)).grid(row=0, column=0, sticky="w", pady=0)
            dry_combo = ttk.Combobox(
                self.settings_container,
                textvariable=self.dryer_settings["dry_level"],
                values=["Very Dry", "More Dry", "Normal Dry", "Damp Dry"],
                state="readonly",
                width=12,
                font=("TkDefaultFont", 7)
            )
            dry_combo.grid(row=0, column=1, sticky="ew", padx=(2, 0), pady=0)

            ttk.Label(self.settings_container, text="Temp:", font=("TkDefaultFont", 7)).grid(row=1, column=0, sticky="w", pady=0)
            temp_combo = ttk.Combobox(
                self.settings_container,
                textvariable=self.dryer_settings["temp"],
                values=["High", "Medium", "Low", "Extra Low"],
                state="readonly",
                width=12,
                font=("TkDefaultFont", 7)
            )
            temp_combo.grid(row=1, column=1, sticky="ew", padx=(2, 0), pady=0)

            ttk.Label(self.settings_container, text="Time:", font=("TkDefaultFont", 7)).grid(row=2, column=0, sticky="w", pady=0)
            time_combo = ttk.Combobox(
                self.settings_container,
                textvariable=self.dryer_settings["time"],
                values=["60min", "40min", "20min"],
                state="readonly",
                width=12,
                font=("TkDefaultFont", 7)
            )
            time_combo.grid(row=2, column=1, sticky="ew", padx=(2, 0), pady=0)

            # Remove filename update bindings - filename generated on Start

        self.settings_container.columnconfigure(1, weight=1)

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        """Build right panel with plot and status."""
        # Plot
        plot_frame = ttk.LabelFrame(parent, text="Real-time Plot", padding=4)
        plot_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        self.fig = Figure(figsize=(7, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Elapsed Time (s)", fontsize=9)
        self.ax.set_ylabel("Current (A)", fontsize=9)
        self.ax.grid(True)
        self.ax.set_title("Current Sensor Data", fontsize=10)

        self.plot_lines = {
            "ct_clamp": self.ax.plot([], [], label="CT Clamp", color="blue", picker=5)[0],
            "hall1": self.ax.plot([], [], label="Hall1", color="green", picker=5)[0],
            "hall2": self.ax.plot([], [], label="Hall2", color="red", picker=5)[0],
        }
        self.ax.legend(loc="upper right", fontsize=8)

        # Store data arrays for hover tooltip
        self.plot_data = {
            "ct_clamp": {"times": [], "values": []},
            "hall1": {"times": [], "values": []},
            "hall2": {"times": [], "values": []},
        }

        # Create annotation for tooltip
        self.tooltip_annotation = self.ax.annotate(
            "",
            xy=(0, 0),
            xytext=(10, 10),
            textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="yellow", alpha=0.8),
            arrowprops=dict(arrowstyle="->", connectionstyle="arc3,rad=0"),
            fontsize=8,
            visible=False,
        )

        self.canvas = FigureCanvasTkAgg(self.fig, plot_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Connect mouse motion event
        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
        self.canvas.mpl_connect("axes_leave_event", self._on_axes_leave)

        # Status/Log
        status_frame = ttk.LabelFrame(parent, text="Status", padding=4)
        status_frame.pack(fill=tk.X)

        ttk.Label(status_frame, textvariable=self.status_var, font=("TkDefaultFont", 8)).pack(anchor="w")

        self.log_text = tk.Text(status_frame, height=4, wrap="word", font=("TkDefaultFont", 8))
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        self.log_text.configure(state=tk.DISABLED)

    def _generate_filename(self) -> str:
        """Generate filename based on current settings and timestamp."""
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")

        machine = self.machine_type.get()
        if machine == "Washer":
            cycle = self.washer_settings["cycle"].get()
            temp = self.washer_settings["temp"].get()
            spin = self.washer_settings["spin"].get()
            filename = f"{timestamp}_{machine}_{cycle}_{temp}_{spin}.csv"
        else:  # Dryer
            dry_level = self.dryer_settings["dry_level"].get().replace(" ", "")
            temp = self.dryer_settings["temp"].get()
            time_val = self.dryer_settings["time"].get()
            filename = f"{timestamp}_{machine}_{dry_level}_{temp}_{time_val}.csv"

        # Return full path to selected folder
        import os
        save_path = self.path_var.get() if self.path_var else self.default_downloads_path
        full_path = os.path.join(save_path, filename)
        return full_path

    def _on_machine_type_changed(self) -> None:
        """Handle machine type change."""
        self._build_settings_dropdowns()

    def _on_rate_changed(self, rate_str: str) -> None:
        """Handle sampling rate change."""
        self.sample_interval = 1.0 / float(rate_str)


    def _on_scan(self) -> None:
        """Scan for BLE devices."""
        self.status_var.set("Scanning...")
        self.device_list.delete(0, tk.END)
        self._devices = []
        self.worker.scan(self.name_var.get().strip(), float(self.timeout_var.get()))

    def _selected_device(self) -> Optional[FoundDevice]:
        """Get selected device from list."""
        sel = self.device_list.curselection()
        if not sel:
            return None
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._devices):
            return None
        return self._devices[idx]

    def _on_connect(self) -> None:
        """Connect to selected device."""
        d = self._selected_device()
        if not d:
            self.status_var.set("Select a device first.")
            return
        self.status_var.set(f"Connecting to {d.address}...")
        self.worker.connect(d.address)

    def _on_disconnect(self) -> None:
        """Disconnect from device."""
        self.worker.disconnect()

    def _on_state_button(self, state: str) -> None:
        """Handle state button click."""
        if not self.is_logging:
            self._append_log(f"State change ignored: Not logging.\n")
            return

        elapsed = time.time() - self.logging_start_time
        self.current_state = state
        self.current_state_var.set(state)  # Update display
        self.state_changes.append(StateChange(time.time(), state, elapsed))
        self._append_log(f"State changed to: {state} (t={elapsed:.1f}s)\n")
    
    def _on_select_path(self) -> None:
        """Open dialog to select save directory."""
        import os
        current_path = self.path_var.get() if self.path_var else self.default_downloads_path
        
        # Use askdirectory to select a folder
        selected_path = filedialog.askdirectory(
            initialdir=current_path if os.path.exists(current_path) else self.default_downloads_path,
            title="Select Save Directory"
        )
        
        if selected_path:
            self.path_var.set(selected_path)
            self.default_downloads_path = selected_path
            self._append_log(f"Save path set to: {selected_path}\n")

    def _on_start_logging(self) -> None:
        """Start logging data to CSV."""
        if not self.connected_var.get():
            messagebox.showerror("Error", "Not connected to device.")
            return

        # Generate filename when Start is clicked
        filename = self._generate_filename()
        self.filename_var.set(filename)  # Store for reference

        try:
            import os
            # Ensure Downloads directory exists
            os.makedirs(self.default_downloads_path, exist_ok=True)
            
            self.csv_file_handle = open(filename, "w", newline="")
            self.csv_file = csv.writer(self.csv_file_handle)
            self.csv_file.writerow(
                ["timestamp", "ct_clamp", "hall1", "hall2", "state", "elapsed_time"]
            )

            self.logging_start_time = time.time()
            self.is_logging = True
            self.current_state = "Off"
            self.current_state_var.set("Off")  # Update display
            self.state_changes = []
            self.data_buffer.clear()
            self.last_sample_time = 0.0
            self.pending_data = {"ct_clamp": None, "hall1": None, "hall2": None}

            self.start_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
            self.export_btn.configure(state=tk.DISABLED)  # Disable export while logging
            self.status_var.set(f"Logging to {os.path.basename(filename)}")
            self._append_log(f"Started logging at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._append_log(f"File: {filename}\n")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start logging: {e}")
            if self.csv_file_handle:
                self.csv_file_handle.close()
                self.csv_file_handle = None
                self.csv_file = None

    def _on_stop_logging(self) -> None:
        """Stop logging and close CSV file."""
        self.is_logging = False
        if self.csv_file_handle:
            self.csv_file_handle.close()
            self.csv_file_handle = None
            self.csv_file = None

        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        # Enable export if there's data
        self.export_btn.configure(state=tk.NORMAL if len(self.data_buffer) > 0 else tk.DISABLED)
        self.status_var.set("Logging stopped")
        self._append_log(f"Stopped logging at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    def _on_export(self) -> None:
        """Export data buffer to CSV file."""
        if len(self.data_buffer) == 0:
            messagebox.showwarning("Warning", "No data to export.")
            return

        # Generate suggested filename based on current settings
        import os
        suggested_filename = self._generate_filename()
        suggested_basename = os.path.basename(suggested_filename)

        # Open file dialog, defaulting to Downloads folder
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=self.default_downloads_path,
            initialfile=suggested_basename,
            title="Export CSV File",
        )

        if not filename:
            return  # User cancelled

        try:
            with open(filename, "w", newline="") as f:
                writer = csv.writer(f)
                # Write header
                writer.writerow(
                    ["timestamp", "ct_clamp", "hall1", "hall2", "state", "elapsed_time"]
                )
                # Write all data samples
                for sample in self.data_buffer:
                    writer.writerow(
                        [
                            datetime.fromtimestamp(sample.timestamp).isoformat(),
                            sample.ct_clamp if sample.ct_clamp is not None else "",
                            sample.hall1 if sample.hall1 is not None else "",
                            sample.hall2 if sample.hall2 is not None else "",
                            sample.state,
                            f"{sample.elapsed_time:.3f}",
                        ]
                    )

            self.status_var.set(f"Exported {len(self.data_buffer)} samples to {filename}")
            self._append_log(f"Exported {len(self.data_buffer)} samples to {filename}\n")
            messagebox.showinfo("Success", f"Successfully exported {len(self.data_buffer)} samples to:\n{filename}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to export CSV: {e}")
            self._append_log(f"Export failed: {e}\n")

    def _append_log(self, text: str) -> None:
        """Append text to log area."""
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_events(self) -> None:
        """Poll BLE events and process data."""
        try:
            while True:
                evt, payload = self.events.get_nowait()
                if evt == "scan_result":
                    self._devices = list(payload)
                    self.device_list.delete(0, tk.END)
                    for d in self._devices:
                        rssi = f"{d.rssi} dBm" if d.rssi is not None else "n/a"
                        self.device_list.insert(
                            tk.END, f"{d.name:20s}  {d.address}  RSSI={rssi}"
                        )
                    self.status_var.set(f"Scan complete. Found {len(self._devices)} device(s).")
                    if self._devices:
                        self.device_list.selection_set(0)
                elif evt == "rx":
                    data = payload
                    parsed = self.parser.parse(data)
                    # Update pending data
                    for key, value in parsed.items():
                        if value is not None:
                            self.pending_data[key] = value

                    # Sample at configured rate
                    now = time.time()
                    if self.is_logging and (now - self.last_sample_time) >= self.sample_interval:
                        self._process_sample(now)
                        self.last_sample_time = now
                elif evt == "status":
                    self.status_var.set(str(payload))
                elif evt == "connected":
                    connected = bool(payload)
                    self.connected_var.set(connected)
                    self.disconnect_btn.configure(state=tk.NORMAL if connected else tk.DISABLED)
                    if not connected and self.is_logging:
                        self._on_stop_logging()
                elif evt == "error":
                    self._append_log(f"[error] {payload}\n")
                    self.status_var.set("Error (see log).")
        except queue.Empty:
            pass

        self.after(50, self._poll_events)

    def _calculate_calibration(self, sensor: str = "ct_clamp") -> None:
        """Calculate calibration factor from multimeter and sensor readings.
        
        Args:
            sensor: One of "ct_clamp", "hall1", "hall2"
        """
        try:
            if sensor == "ct_clamp":
                mm_reading = float(self.mm_reading_var.get())
                sensor_reading = float(self.clamp_reading_var.get())
                calib_var = self.ct_calib_var
                sensor_name = "CT Clamp"
            elif sensor == "hall1":
                mm_reading = float(self.hall1_mm_var.get())
                sensor_reading = float(self.hall1_sensor_var.get())
                calib_var = self.hall1_calib_var
                sensor_name = "Hall1"
            elif sensor == "hall2":
                mm_reading = float(self.hall2_mm_var.get())
                sensor_reading = float(self.hall2_sensor_var.get())
                calib_var = self.hall2_calib_var
                sensor_name = "Hall2"
            else:
                messagebox.showerror("Error", f"Unknown sensor: {sensor}")
                return
            
            if sensor_reading == 0:
                messagebox.showerror("Error", "Sensor reading cannot be zero.")
                return
            
            factor = mm_reading / sensor_reading
            calib_var.set(f"{factor:.4f}")
            self._update_calibration()
            self._append_log(f"Calculated {sensor_name} factor: {factor:.4f} (MM: {mm_reading}A / Sensor: {sensor_reading}A)\n")
        except ValueError:
            messagebox.showerror("Error", "Please enter valid numbers for both readings.")
        except Exception as e:
            messagebox.showerror("Error", f"Calculation failed: {e}")

    def _update_calibration(self) -> None:
        """Update calibration factors from UI for all sensors."""
        try:
            self.calibration_factors["ct_clamp"] = float(self.ct_calib_var.get())
        except ValueError:
            pass  # Invalid input, keep previous value
        
        try:
            self.calibration_factors["hall1"] = float(self.hall1_calib_var.get())
        except ValueError:
            pass  # Invalid input, keep previous value
        
        try:
            self.calibration_factors["hall2"] = float(self.hall2_calib_var.get())
        except ValueError:
            pass  # Invalid input, keep previous value

    def _process_sample(self, timestamp: float) -> None:
        """Process a data sample and write to CSV."""
        if not self.logging_start_time:
            return

        elapsed = timestamp - self.logging_start_time

        # Get current state (carry forward until next change)
        state = self.current_state

        # Apply calibration factors
        ct_value = self.pending_data["ct_clamp"]
        h1_value = self.pending_data["hall1"]
        h2_value = self.pending_data["hall2"]
        
        if ct_value is not None:
            ct_value *= self.calibration_factors["ct_clamp"]
        if h1_value is not None:
            h1_value *= self.calibration_factors["hall1"]
        if h2_value is not None:
            h2_value *= self.calibration_factors["hall2"]

        sample = DataSample(
            timestamp=timestamp,
            ct_clamp=ct_value,
            hall1=h1_value,
            hall2=h2_value,
            state=state,
            elapsed_time=elapsed,
        )

        self.data_buffer.append(sample)

        # Enable export button when we have data
        if len(self.data_buffer) == 1 and not self.is_logging:
            self.export_btn.configure(state=tk.NORMAL)

        # Write to CSV
        if self.csv_file:
            self.csv_file.writerow(
                [
                    datetime.fromtimestamp(timestamp).isoformat(),
                    sample.ct_clamp if sample.ct_clamp is not None else "",
                    sample.hall1 if sample.hall1 is not None else "",
                    sample.hall2 if sample.hall2 is not None else "",
                    sample.state,
                    f"{elapsed:.3f}",
                ]
            )
            self.csv_file_handle.flush()

    def _update_plot(self) -> None:
        """Update the real-time plot."""
        if len(self.data_buffer) == 0:
            self.after(100, self._update_plot)
            return

        # Extract data
        times = [s.elapsed_time for s in self.data_buffer]
        ct_data = [s.ct_clamp if s.ct_clamp is not None else float("nan") for s in self.data_buffer]
        h1_data = [s.hall1 if s.hall1 is not None else float("nan") for s in self.data_buffer]
        h2_data = [s.hall2 if s.hall2 is not None else float("nan") for s in self.data_buffer]

        # Store data for hover tooltip
        self.plot_data["ct_clamp"] = {"times": times, "values": ct_data}
        self.plot_data["hall1"] = {"times": times, "values": h1_data}
        self.plot_data["hall2"] = {"times": times, "values": h2_data}

        # Update plot lines based on visibility
        if self.sensor_vars["ct_clamp"].get():
            self.plot_lines["ct_clamp"].set_data(times, ct_data)
            self.plot_lines["ct_clamp"].set_visible(True)
        else:
            self.plot_lines["ct_clamp"].set_visible(False)

        if self.sensor_vars["hall1"].get():
            self.plot_lines["hall1"].set_data(times, h1_data)
            self.plot_lines["hall1"].set_visible(True)
        else:
            self.plot_lines["hall1"].set_visible(False)

        if self.sensor_vars["hall2"].get():
            self.plot_lines["hall2"].set_data(times, h2_data)
            self.plot_lines["hall2"].set_visible(True)
        else:
            self.plot_lines["hall2"].set_visible(False)

        # Auto-scale axes - use sliding window for Y-axis to zoom back in when peaks disappear
        if times:
            # X-axis: show last 60 seconds
            x_min = max(0, times[-1] - 60)
            x_max = max(60, times[-1] + 5)
            self.ax.set_xlim(x_min, x_max)

            # Y-axis: only consider data in the visible time window (last 60 seconds)
            window_start_time = times[-1] - 60 if len(times) > 0 else 0
            visible_indices = [i for i, t in enumerate(times) if t >= window_start_time]
            
            if visible_indices:
                # Extract visible data only
                visible_values = []
                if self.sensor_vars["ct_clamp"].get():
                    visible_ct = [ct_data[i] for i in visible_indices if not (ct_data[i] != ct_data[i])]
                    visible_values.extend(visible_ct)
                if self.sensor_vars["hall1"].get():
                    visible_h1 = [h1_data[i] for i in visible_indices if not (h1_data[i] != h1_data[i])]
                    visible_values.extend(visible_h1)
                if self.sensor_vars["hall2"].get():
                    visible_h2 = [h2_data[i] for i in visible_indices if not (h2_data[i] != h2_data[i])]
                    visible_values.extend(visible_h2)

                if visible_values:
                    y_min = min(visible_values)
                    y_max = max(visible_values)
                    margin = (y_max - y_min) * 0.1 if y_max > y_min else 1.0
                    self.ax.set_ylim(max(0, y_min - margin), y_max + margin)
                else:
                    # Fallback: use all data if no visible values
                    all_values = []
                    if self.sensor_vars["ct_clamp"].get():
                        all_values.extend([v for v in ct_data if not (v != v)])
                    if self.sensor_vars["hall1"].get():
                        all_values.extend([v for v in h1_data if not (v != v)])
                    if self.sensor_vars["hall2"].get():
                        all_values.extend([v for v in h2_data if not (v != v)])
                    
                    if all_values:
                        y_min = min(all_values)
                        y_max = max(all_values)
                        margin = (y_max - y_min) * 0.1 if y_max > y_min else 1.0
                        self.ax.set_ylim(max(0, y_min - margin), y_max + margin)

        self.canvas.draw()
        self.after(100, self._update_plot)

    def _on_mouse_move(self, event) -> None:
        """Handle mouse movement over the plot."""
        if event.inaxes != self.ax:
            self.tooltip_annotation.set_visible(False)
            self.canvas.draw_idle()
            return

        if len(self.data_buffer) == 0:
            return

        # Get mouse position in data coordinates
        mouse_x = event.xdata
        mouse_y = event.ydata

        if mouse_x is None or mouse_y is None:
            return

        # Find the nearest point on any visible line
        min_distance = float("inf")
        best_sensor = None
        best_index = None
        best_time = None
        best_value = None

        sensor_names = {
            "ct_clamp": "CT Clamp",
            "hall1": "Hall1",
            "hall2": "Hall2",
        }

        for sensor_key, sensor_name in sensor_names.items():
            if not self.sensor_vars[sensor_key].get():
                continue

            times = self.plot_data[sensor_key]["times"]
            values = self.plot_data[sensor_key]["values"]

            if len(times) == 0:
                continue

            # Find nearest point
            for i, (t, v) in enumerate(zip(times, values)):
                if v != v:  # Skip NaN values
                    continue

                # Calculate distance from mouse to this point
                dx = t - mouse_x
                dy = v - mouse_y
                # Scale by axis ranges for better distance calculation
                x_range = self.ax.get_xlim()[1] - self.ax.get_xlim()[0]
                y_range = self.ax.get_ylim()[1] - self.ax.get_ylim()[0]
                if x_range == 0 or y_range == 0:
                    continue

                distance = ((dx / x_range) ** 2 + (dy / y_range) ** 2) ** 0.5

                if distance < min_distance:
                    min_distance = distance
                    best_sensor = sensor_name
                    best_index = i
                    best_time = t
                    best_value = v

        # Show tooltip if we found a close point (within reasonable distance)
        if best_sensor is not None and min_distance < 0.05:  # Threshold for showing tooltip
            self.tooltip_annotation.xy = (best_time, best_value)
            self.tooltip_annotation.set_text(
                f"{best_sensor}\nTime: {best_time:.3f} s\nCurrent: {best_value:.4f} A"
            )
            self.tooltip_annotation.set_visible(True)
        else:
            self.tooltip_annotation.set_visible(False)

        self.canvas.draw_idle()

    def _on_axes_leave(self, event) -> None:
        """Hide tooltip when mouse leaves the axes."""
        self.tooltip_annotation.set_visible(False)
        self.canvas.draw_idle()

    def _on_close(self) -> None:
        """Handle window close."""
        if self.is_logging:
            self._on_stop_logging()
        try:
            self.worker.shutdown()
        finally:
            self.master.destroy()


def main() -> None:
    """Main entry point."""
    root = tk.Tk()
    ttk.Style().theme_use("clam")
    app = LaundryIQLogger(root)
    root.mainloop()


if __name__ == "__main__":
    main()

