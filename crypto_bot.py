if hit:
                        arrow = "≥" if a.direction == "above" else "≤"
                        text = (
                            "🔔 Сработало уведомление!\n"
                            f"#{a.alert_id} {a.symbol}\n"
                            f"Условие: {arrow} {a.target}\n"
                            f"Текущая: {cur:.2f}"
                        )
                        try:
                            await app.bot.send_message(chat_id=user_id, text=text, reply_markup=kb_after_alert_created())
                        except Exception:
                            pass

                        # удаляем после срабатывания
                        delete_alert(user_id, a.alert_id)

        except Exception:
            # чтобы цикл не падал
            pass

        await asyncio.sleep(CHECK_ALERTS_EVERY_SECONDS)


async def post_init(app):
    load_alerts()
    app.create_task(alerts_loop(app))


# ===================== RUN =====================

def main():
    if not TOKEN:
        # чтобы на Render было видно причину в логах
        raise RuntimeError("❌ Не найден BOT_TOKEN. Добавь переменную окружения BOT_TOKEN в Render.")

    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(buttons))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("✅ Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if name == "main":
    main()
