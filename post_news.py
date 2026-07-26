#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Базовые тесты для news_bot_fixed.py — признак 8 из списка внедрённых
рекомендаций. Не требуют сети/токенов, проверяют только чистую логику:
дедуп (та часть, что и была причиной дублей в проде) и санитаризацию
текста. Запуск: python3 test_news_bot.py
"""

import importlib.util
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("news_bot", os.path.join(HERE, "news_bot_fixed.py"))
bot = importlib.util.module_from_spec(spec)
sys.modules["news_bot"] = bot
spec.loader.exec_module(bot)

passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


print("=== Дедуп: точное совпадение заголовка ===")
k1 = bot.title_dedup_key("Месси вернулся в Росарио для восстановления")
k2 = bot.title_dedup_key("месси вернулся в росарио для восстановления!")
k3 = bot.title_dedup_key("Совсем другая новость про футбол")
check("одинаковый текст в разном регистре/пунктуации даёт один ключ", k1 == k2)
check("разный текст даёт разные ключи", k1 != k3)

print("\n=== Дедуп: похожесть по смыслу (title+summary) ===")
words_a = bot.content_words(
    "Месси вернулся в Росарио для восстановления",
    "Лионель Месси прибыл в родной город Росарио для реабилитации после ЧМ",
)
words_b = bot.content_words(
    "Месси вернулся в Росарио после ЧМ",
    "Лионель Месси прибыл в родной город Росарио для восстановления",
)
words_c = bot.content_words(
    "ЦБ повысил ключевую ставку",
    "Банк России принял решение по инфляции",
)
check(
    "разные заголовки про одно и то же событие распознаются как похожие "
    "(это была причина дублей на проде — см. скриншот с Месси)",
    bot.titles_are_similar(words_a, words_b),
)
check("явно разные новости не считаются похожими", not bot.titles_are_similar(words_a, words_c))

print("\n=== Дедуп: is_duplicate_by_meaning ===")
recent = [words_b]
check("новость находит совпадение в списке недавних", bot.is_duplicate_by_meaning(words_a, recent))
check("новость не находит совпадение в пустом списке", not bot.is_duplicate_by_meaning(words_a, []))

print("\n=== Санитаризация текста ===")
long_title = ("Это очень длинный тестовый заголовок новости про важное событие "
              "в стране, который специально сделан длиннее текущего лимита "
              "HEADLINE_MAX_LEN, чтобы проверить обрезку по границе слова")
truncated = bot.truncate_at_word(long_title)
check(
    "truncate_at_word укладывается в лимит и обрезает по границе слова (многоточие в конце)",
    len(truncated) <= bot.HEADLINE_MAX_LEN and truncated.endswith("…"),
)
check(
    "заголовок короче лимита не обрезается",
    bot.truncate_at_word("Короткий заголовок") == "Короткий заголовок",
)
check(
    "strip_cliche_openers убирает вводное клише",
    bot.strip_cliche_openers("Как сообщается, президент подписал указ") == "Президент подписал указ",
)
check(
    "strip_cliche_openers не трогает текст без клише",
    bot.strip_cliche_openers("Президент подписал указ") == "Президент подписал указ",
)
check(
    "fix_shouty_caps приводит капс к обычному регистру",
    bot.fix_shouty_caps("ПУТИН ПОДПИСАЛ ЗАКОН") == "Путин подписал закон",
)
check(
    "fix_shouty_caps не трогает обычный текст",
    bot.fix_shouty_caps("Путин подписал закон") == "Путин подписал закон",
)
check(
    "strip_decorative_emoji убирает эмодзи",
    "🔥" not in bot.strip_decorative_emoji("🔥 Важная новость 🚀"),
)
check(
    "collapse_repeated_punctuation схлопывает повторы",
    bot.collapse_repeated_punctuation("Правда?!!!") == "Правда?!",
)

print("\n=== Категории ===")
cat, tag = bot.detect_category("ЦБ повысил ключевую ставку", "решение по инфляции")
check("экономическая новость определяется как #экономика", tag == "#экономика")
cat, tag = bot.detect_category("Сборная выиграла матч чемпионата", "")
check("спортивная новость определяется как #спорт", tag == "#спорт")
cat, tag = bot.detect_category("Название без ключевых слов вообще", "")
check("неопознанная категория возвращает None вместо хэштега", tag is None)

print("\n=== Ограничение длины предложений ===")
long_sentence = "Это очень длинное предложение, которое содержит " + "слово " * 25 + "и должно быть разбито на части."
result = bot.split_long_sentences(long_sentence, max_words=20)
check("длинное предложение с запятой разбивается на два", result.count(".") >= 2)

print("\n=== Реальный кейс из продакшена: дубль про Эльбрус (падежные окончания) ===")
words_elbrus_a = bot.content_words(
    "Эвакуированы тела погибших на Эльбрусе альпинистов",
    "Спасатели МЧС вывезли тела двух погибших боснийцев с Эльбруса. "
    "Поиск оставшихся троих перенесли из-за плохой погоды на 27 июля.",
)
words_elbrus_b = bot.content_words(
    "Спасатели эвакуировали тела погибших на Эльбрусе",
    "В МЧС сообщили, что тела двух погибших боснийских альпинистов "
    "эвакуированы с Эльбруса. Поиск оставшихся троих временно "
    "приостановлен из-за плохой погоды.",
)
check(
    "падежные формы 'Эльбрусе'/'Эльбруса' и 'альпинистов'/'боснийских' "
    "теперь распознаются как один и тот же дубль (это был реальный "
    "пропущенный дубль на проде — см. скриншот)",
    bot.titles_are_similar(words_elbrus_a, words_elbrus_b),
)

print("\n=== Ложные срабатывания категории #технологии убраны ===")
_, tag = bot.detect_category(
    "Пропавшие в Паттайе тюменцы",
    "Родительница накануне при срабатывании трекера слежения на своём телефоне.",
)
check(
    "слово 'срабатывании' (оканчивается на -ии) больше не триггерит #технологии",
    tag != "#технологии",
)

print(f"\n{'='*40}\nИтого: {passed} прошло, {failed} провалено")
if failed:
    sys.exit(1)
