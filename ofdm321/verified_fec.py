# Copied unchanged from OFDM_Video_N210 v1.23 (ofdmvideo). Wire format kept identical.
"""Withhold all DATA until overdetermined RS8+8 parity checks succeed.

The pilot_chase path can repair CRC-aided candidates. Unlike the ordinary
erasure receiver, it requires k+2 shards for a k-DATA group and compares the
decoded bytes against all received parity. It never releases an unchecked
candidate, including at END or the next group boundary.
"""
from .fec_rs import RSReceiverState, DATA_COUNT, NotEnoughShards, recover_payloads, encode_parity
from .protocol import DATA, pack_packet, unpack_packet


class RSParityCheckedState(RSReceiverState):
    def __init__(self, session):
        super().__init__(session, parity_count=8)
        self.verified_groups = self.parity_rejected_groups = 0
        self.insufficient_check_groups = self.extra_parity_checks = 0

    def _flush(self, count=DATA_COUNT, close_reason="group_boundary"):
        if self.base is None:
            return b""
        data = {seq - self.base: unpack_packet(raw)[4] for seq, raw in self.pending.items()
                if self.base <= seq < self.base + count}
        shards = len(data) + len(self.parity)
        if shards < count + 2 and close_reason == "enough_shards":
            return b""
        decoded = None
        reason = "insufficient_extra_parity"
        if shards >= count + 2:
            try:
                candidate = recover_payloads(data, self.parity, count)
                reference = encode_parity(candidate, 8)
                if all(reference[index] == value for index, value in self.parity.items()):
                    decoded = candidate
                    self.verified_groups += 1
                    self.extra_parity_checks += len(self.parity) - (count - len(data))
                else:
                    reason = "parity_mismatch"
                    self.parity_rejected_groups += 1
            except NotEnoughShards:
                pass
        else:
            self.insufficient_check_groups += 1
        output = []
        if decoded is not None:
            for i, payload in enumerate(decoded):
                raw = self.pending.get(self.base + i)
                if raw is None:
                    raw = pack_packet(DATA, self.inner.session, self.base + i, payload)
                    self.recovered += 1
                delivered = self.inner.ingest(raw)
                if delivered is not None:
                    output.append(delivered)
        else:
            self.failed_groups += 1
            if len(self.failed_group_details) < 64:
                self.failed_group_details.append({
                    "base_data_sequence": self.base, "data_count": count,
                    "received_data_sequences": [self.base + i for i in sorted(data)],
                    "missing_data_sequences": list(range(self.base, self.base + count)),
                    "received_parity_indices": sorted(self.parity),
                    "valid_shards": shards, "required_shards": count + 2,
                    "parity_packets_per_group": 8, "close_reason": close_reason,
                    "verification_rejection_reason": reason,
                    "note": "Entire group withheld: no unverified DATA released"})
            else:
                self.failed_group_details_truncated = True
        self.closed_before = self.base + count
        self.inner.confirm_until(self.closed_before)
        self.base, self.pending, self.parity, self.count = None, {}, {}, None
        return b"".join(output)

    def snapshot(self):
        return {**super().snapshot(), "fec_verification_mode": "two_extra_rs_parity_checks",
                "fec_minimum_extra_shards": 2, "fec_verified_groups": self.verified_groups,
                "fec_extra_parity_checks": self.extra_parity_checks,
                "fec_parity_mismatch_groups": self.parity_rejected_groups,
                "fec_insufficient_extra_parity_groups": self.insufficient_check_groups,
                "data_packets_received_directly_scope": "Includes CRC-aided candidates; all delivered groups pass extra RS checks",
                "fec_integrity_note": "Full group needs 10/16 shards, not 8/16; unverified groups are discarded"}
