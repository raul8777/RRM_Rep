param(
    [string]$Symbols = "ENAUSDT,ADAUSDT",
    [double]$BufferUsdt = 0.30,
    [string]$CloseReason = "manual_force_close_dust_runner",
    [double]$CommissionRate = 0.001
)

docker exec crypto-agent-app python /app/Dev/tools/spot_close/force_close_dust.py --symbols $Symbols --buffer-usdt $BufferUsdt --close-reason $CloseReason --commission-rate $CommissionRate
