p='/app/run_trades.py'
with open(p, 'r', encoding='utf-8') as f:
    t=f.read()
t = t.replace(
    '                                except Exception as _sso_s_err:\n                                    print(f"[SSO/SPOT] {symbol} partial close error: {_sso_s_err}")\n                                    print(f"[SSO/SPOT] {symbol} partial close error: {_sso_s_err}")',
    '                                except Exception as _sso_s_err:\n                                    print(f"[SSO/SPOT] {symbol} partial close error: {_sso_s_err}")'
)
with open(p, 'w', encoding='utf-8') as f:
    f.write(t)
print('fixed duplicate')
