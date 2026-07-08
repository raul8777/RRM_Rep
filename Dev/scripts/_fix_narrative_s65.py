"""F2-A + F3-A: Fix NARRATIVE category_thresholds — min_prob 0.527→0.42, trailing_distance 2.0→3.0"""

with open(r's:\Tools\AI\crypto-agent\config.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

TARGET_LINE = 486  # 0-indexed = line 487
old_line = lines[TARGET_LINE]
print(f"OLD: {repr(old_line[:100])}")

new_line = (
    '        "NARRATIVE":  {"min_prob": 0.42,  "min_pnl": -1.0, "min_volspike": 1.5, '
    '"stop_loss_pct": 3.5, "take_profit_pct": 7.0, "trailing_activation_pct": 5.0, '
    '"trailing_distance_pct": 3.0, "tp_extension_pct": 3.0},'
    '  # s65: min_prob 0.527->0.42 (asimetrico per diseno); trail_dist 2.0->3.0 (captura breakouts +15-30%)\n'
)

assert 'NARRATIVE' in old_line and 'min_prob' in old_line, "Unexpected content at line 487"
lines[TARGET_LINE] = new_line

with open(r's:\Tools\AI\crypto-agent\config.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print(f"NEW: {repr(new_line[:100])}")
print("DONE")
