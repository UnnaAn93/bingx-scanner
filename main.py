                # 2. Визначення зміни тренду / розвороту
                elif trend_shift_pct <= -1.5 and current_close > local_resistance and current_volume >= avg_volume * 1.2:
                    alerts.append(f"⚡ **{symbol} (15m)**: **ЗМІНА ТРЕНДУ (Розворот вгору)** (вікно {L}) через опір {local_resistance:.4f}.")
                    signal_triggered = True
                    break
                elif trend_shift_pct >= 1.5 and current_close < local_support and current_volume >= avg_volume * 1.2:
                    alerts.append(f"⚡ **{symbol} (15m)**: **ЗМІНА ТРЕНДУ (Розворот вниз)** (вікно {L}) через підтримку {local_support:.4f}.")
                    signal_triggered = True
                    break
