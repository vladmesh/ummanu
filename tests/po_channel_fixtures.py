"""Administrative PO mentions shared by bounded-grammar and SQL admission tests."""

ADMINISTRATIVE_PO_NOTES = (
    "Ожидаем ответа по CI.", "Ждём по ummanu-73 зелёного CI.",
    "Дождаться по ummanu-73 вердикта ревьюера.", "Ждать по плану диспетчера.",
    "Прошу по возможности проверить CI.", "Нужен ответ по ревью ummanu-73.",
    "Требуется решение по маршруту хотфикса.", "Wait for PO-channel CI on ummanu-73.",
    "Wait for PO session rollover card ummanu-80 to merge.", "Need PO input compaction (DoD5) next.",
    "Await PO service restart before resubmitting.", "Request PO-input fixture repair from the worker.",
    "Need PO context rollover (DoD6) next.", "Need PO context rollover next.",
    "Wait for PO rollover card ummanu-80 to merge.", "Need PO feed compaction (DoD5).",
    "Need PO compact input (DoD5).", "Wait for PO handover fix to merge.",
    "Need PO dashboard repair.", "Wait for PO turns to drain.", "Wait for PO sessions to close.",
    "Ждём ПО сессию.",
    # Previously unlisted modifiers demonstrate the default, in both languages.
    "Need PO telemetry repair.", "Ждём ПО телеметрию.",
    "Need PO's dashboard repair.",
    "Need PO answer delivery fix (DoD5).", "Need PO approval flow rework.",
    "Wait for PO decision card ummanu-80 to merge.", "PO review fix ummanu-80 is in CI.",
    "PO answer delivery is fixed.",
)

REQUEST_PO_NOTES = (
    "[observer:request] Assign the route", "Please ask PO to approve the route.",
    "PO, please decide.", "Wait for the PO decision.", "We need PO to assign this.",
    "Need a decision from PO.", "Прошу ПО назначить маршрут.", "Ждём решения ПО.",
    "Нужно решение ПО.", "ПО должен выбрать маршрут.", "Следующий шаг: ждать ответа ПО.",
    "Need PO.", "Wait for PO's approval.", "PO must decide.", "PO to approve.",
    "Request PO answer.", "Need Product Owner to resolve this.",
    "Ждём ПО ответ.", "Прошу ПО решить вопрос.", "ПО, назначь маршрут.",
    "Need a decision from PO on the route.", "Need a decision from the PO about retry.",
    "Ask the PO for a decision.", "Ask PO to pick the route.", "Need PO to pick the route.",
    "PO must pick the route.", "Нужно решение ПО по маршруту.",
    "Ждём решения ПО по маршруту хотфикса.", "Ждём ответа ПО на вопрос.",
    "Требуется согласование ПО на повтор.",
    # Actor frames do not depend on a vocabulary of action verbs/complements.
    "Need PO to reconcile the contracts.", "Request PO to prioritize the investigation.",
    "PO must improve telemetry next.", "Need an answer from PO regarding rollout.",
    "Await approval by the PO before deployment.", "Ждём действия от ПО после расследования.",
    "Требуется решение ПО относительно маршрутизации.", "ПО должен сопоставить результаты.",
    "Нужно, чтобы ПО распределил работу.",
    "Прошу ПО перепроверить ограничения перед повтором.",
)

NEUTRAL_PO_NOTES = (
    "No PO action is needed.", "Do not ask PO to decide.", "Не ждём решения ПО.",
    'Evidence: "Wait for PO decision."', '> [observer:request] historical request',
    "```\nPO, decide now.\n```", "Implement PO routing in the next code card.",
    "Future implementation will validate PO decisions.", "Recorded PO decision is quoted on the card.",
    "Будущая реализация проверяет решения ПО.", "Note: PO session is recorded.",
)

NEUTRAL_PO_NOTES += tuple(f'"{body}"' for body in REQUEST_PO_NOTES)
