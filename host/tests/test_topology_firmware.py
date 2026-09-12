"""Run the actual master topology C state machine with fake peripheral APIs."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from c_toolchain import build_environment, resolve_c_compiler


ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "host"


class MasterTopologyFirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = resolve_c_compiler(ROOT)
        if os.name == "nt" and "clang" in Path(compiler.executable).name.lower():
            targets = subprocess.run([*compiler.command, "--print-targets"],
                                     capture_output=True, text=True, timeout=15).stdout
            if "x86" not in targets:
                raise RuntimeError(
                    f"{compiler.executable} has no native Windows x86 backend, so it cannot "
                    "build the firmware C harnesses. Set CABLE_HOST_CC to a working compiler."
                )
        cls._temporary = tempfile.TemporaryDirectory(prefix="topology_c_", dir=HOST / "tests")
        command = [*compiler.command, "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                   "-Wall", "-Wextra", "-Werror", "-O1", "-shared"]
        if os.name != "nt":
            command += ["-fPIC"]
        command += ["-I", str(HOST / "tests/firmware/topology_stubs")]
        environment = build_environment(compiler, ROOT)
        cls._libraries = []
        try:
            for source in ("topology_scan_test", "topology_fixed_route_test"):
                output = Path(cls._temporary.name) / (source + (".dll" if os.name == "nt" else ".so"))
                subprocess.run(command + [str(HOST / f"tests/firmware/{source}.c"), "-o", str(output)], check=True, capture_output=True, text=True, timeout=120, env=environment)
                cls._libraries.append(ctypes.CDLL(str(output)))
        except subprocess.CalledProcessError as error:
            cls._unload_libraries()
            cls._temporary.cleanup()
            raise RuntimeError(f"Master topology C harness build failed:\n{error.stdout}\n{error.stderr}") from error
        except Exception:
            cls._unload_libraries()
            cls._temporary.cleanup()
            raise

    @classmethod
    def _unload_libraries(cls) -> None:
        if os.name == "nt":
            import _ctypes

            for library in cls._libraries:
                _ctypes.FreeLibrary(library._handle)
        cls._libraries.clear()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._unload_libraries()
        cls._temporary.cleanup()

    def _check(self, name: str) -> None:
        function = next(getattr(library, name) for library in self._libraries if hasattr(library, name))
        function.argtypes = []
        function.restype = ctypes.c_int
        self.assertEqual(function(), 0, f"C assertion failed at returned source line in {name}")

    def test_remote_plan_seal_owner_and_local_mask_addressing(self) -> None:
        self._check("topology_test_remote_plan")

    def test_source_scan_waits_for_ready_and_rejects_stale_peer(self) -> None:
        self._check("topology_test_source_scan")

    def test_cancellation_and_foreign_owner_rejection(self) -> None:
        self._check("topology_test_cancel_and_owner")

    def test_failed_remote_ready_never_closes_source(self) -> None:
        self._check("topology_test_failed_ready")

    def test_cleanup_error_cannot_report_success(self) -> None:
        self._check("topology_test_cleanup_error")

    def test_source_route_and_expansion_bounds(self) -> None:
        self._check("topology_test_role_and_arguments")

    def test_fixed_route_reset_never_pulses_hardware_reset(self) -> None:
        self._check("topology_test_fixed_route_reset")

    def test_fixed_route_rejects_manual_open(self) -> None:
        self._check("topology_test_fixed_route_rejects_open")

    def test_fixed_route_rejects_bus_bridges(self) -> None:
        self._check("topology_test_fixed_route_rejects_bus_bridges")

    def test_fixed_route_connect_rejects_before_reset(self) -> None:
        self._check("topology_test_fixed_route_connect_rejects_before_reset")

    def test_fixed_route_cleans_legacy_bridges(self) -> None:
        self._check("topology_test_fixed_route_cleans_legacy_bridges")

    def test_second_master_reset_opens_every_contact(self) -> None:
        self._check("topology_test_unfixed_master_clears_all")

    def test_disconnect_epoch_rejects_old_session_commands(self) -> None:
        self._check("topology_test_disconnect_rejects_old_session")

    def test_source_lease_rejects_foreign_requests_between_point_jobs(self) -> None:
        self._check("topology_test_source_lease_between_points")

    def test_reliable_samples_commit_without_result_delivery(self) -> None:
        self._check("topology_test_reliable_commit_without_result_delivery")

    def test_reliable_disconnect_before_commit_retries_only_uncommitted_sample(self) -> None:
        self._check("topology_test_reliable_disconnect_before_commit_retries_once")

    def test_reliable_disconnect_after_commit_resumes_at_next_coordinate(self) -> None:
        self._check("topology_test_reliable_disconnect_after_commit_keeps_next_coordinate")

    def test_reliable_fetch_replay_ack_ownership_and_reset_retention(self) -> None:
        self._check("topology_test_reliable_fetch_ack_reset_retention")

    def test_reliable_receiver_retains_plan_but_requires_resume_after_disconnect(self) -> None:
        self._check("topology_test_reliable_receiver_plan_survives_disconnect")

    def test_reliable_cache_pause_needs_acknowledged_data_drain_before_sampling(self) -> None:
        self._check("topology_test_reliable_cache_pause_requires_drain_before_next_sample")

    def test_reliable_duplicate_job_is_idempotent_and_next_point_job_is_distinct(self) -> None:
        self._check("topology_test_reliable_repeated_job_is_idempotent")

    def test_reliable_boot_retains_upload_but_does_not_restart_hardware(self) -> None:
        self._check("topology_test_reliable_boot_recovers_upload_without_resuming_hardware")

    def test_recovered_cache_releases_lock_after_original_module_discovery_without_erasing_records(self) -> None:
        """Run real firmware recovery with one present slave and the boot default seven."""
        self._check("topology_test_recovered_cache_requires_original_module_discovery")

    def test_discovery_cannot_reduce_cleanup_scope_of_an_existing_receiver_plan(self) -> None:
        """Keep both original modules selected until their receiver plan is cleared."""
        self._check("topology_test_discovery_cannot_shrink_existing_receiver_plan")

    def test_reliable_receiver_change_discards_inflight_reading_without_source_disconnect(self) -> None:
        self._check("topology_test_reliable_receiver_change_discards_inflight_sample")

    def test_reliable_receiver_validation_rejects_cleared_replaced_expired_or_disconnected_selection(self) -> None:
        self._check("topology_test_reliable_receiver_selection_validation_lifetime")

    def test_binary_span_crosses_module_boundaries_and_invalidates_old_tokens(self) -> None:
        self._check("topology_test_binary_span_cross_module_and_token")

    def test_binary_range_job_is_idempotent_and_persists_exact_coordinates(self) -> None:
        self._check("topology_test_binary_range_identity_and_durable_record")

    def test_binary_range_reconnect_retries_only_uncommitted_measurement(self) -> None:
        self._check("topology_test_binary_range_disconnect_before_commit")


if __name__ == "__main__":
    unittest.main()
