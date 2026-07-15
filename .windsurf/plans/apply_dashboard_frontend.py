path = '/app/frontend/dashboard.js'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Add totalRealized and totalPnl after totalUnreal in renderOpenPositions
old1 = '''    const totalUnreal = visiblePositions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0);
    const totalInvested = visiblePositions.reduce((s, p) => s + parseFloat(p.invested_usdt || 0), 0);'''
new1 = '''    const totalUnreal = visiblePositions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0);
    const totalRealized = visiblePositions.reduce((s, p) => s + parseFloat(p.realized_pnl || 0), 0);
    const totalPnl = totalUnreal + totalRealized;
    const totalInvested = visiblePositions.reduce((s, p) => s + parseFloat(p.invested_usdt || 0), 0);'''

# 2. Replace the PnL No Real header cell
old2 = '''                <th>PnL No Real.</th>'''
new2 = '''                <th>PnL (No Real / Real / Total)</th>'''

# 3. Replace the row PnL cell with multi-line display
old3 = '''            <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)} USDT</td>'''
new3 = '''            <td class="${pnlClass(p.unrealized_pnl + (p.realized_pnl || 0))}">
                <div class="${pnlClass(p.unrealized_pnl)}">No Real: ${fmt(p.unrealized_pnl)} USDT</div>
                ${parseFloat(p.realized_pnl || 0) !== 0 ? `<div class="${pnlClass(p.realized_pnl)}">Real: ${fmt(p.realized_pnl)} USDT</div>` : ''}
                <div class="${pnlClass((p.unrealized_pnl || 0) + (p.realized_pnl || 0))}"><strong>Total: ${fmt((p.unrealized_pnl || 0) + (p.realized_pnl || 0))} USDT</strong></div>
            </td>'''

# 4. Extend the summary footer
old4 = '''            PnL No Realizado:
            <span class="${pnlClass(totalUnreal)}">
                <strong>${totalUnreal >= 0 ? '+' : ''}${fmt(totalUnreal)} USDT</strong>
            </span>
            ${predPositions.length > 0 ? `&nbsp;|&nbsp;Precisión Pred. Promedio: <strong>${(avgAccuracy * 100).toFixed(0)}%</strong>` : ''}'''
new4 = '''            PnL No Realizado:
            <span class="${pnlClass(totalUnreal)}">
                <strong>${totalUnreal >= 0 ? '+' : ''}${fmt(totalUnreal)} USDT</strong>
            </span>
            &nbsp;|&nbsp; Realizado:
            <span class="${pnlClass(totalRealized)}">
                <strong>${totalRealized >= 0 ? '+' : ''}${fmt(totalRealized)} USDT</strong>
            </span>
            &nbsp;|&nbsp; Total:
            <span class="${pnlClass(totalPnl)}">
                <strong>${totalPnl >= 0 ? '+' : ''}${fmt(totalPnl)} USDT</strong>
            </span>
            ${predPositions.length > 0 ? `&nbsp;|&nbsp;Precisión Pred. Promedio: <strong>${(avgAccuracy * 100).toFixed(0)}%</strong>` : ''}'''

replacements = [(old1, new1), (old2, new2), (old3, new3), (old4, new4)]
for old, new in replacements:
    if old not in text:
        print('MISSING', old[:80])
        raise SystemExit(1)
    text = text.replace(old, new, 1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)
print('dashboard.js patched')
