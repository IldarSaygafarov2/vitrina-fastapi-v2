from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from backend.app.config import config

from infrastructure.database.repo.requests import RequestsRepo
from tgbot.keyboards.admin.inline import (
    advertisement_moderation_kb,
    delete_advertisement_kb,
)
from tgbot.keyboards.user.inline import actual_checking_kb, is_price_actual_kb
from tgbot.misc.user_states import AdvertisementRelevanceState
from tgbot.templates.advertisement_creation import realtor_advertisement_completed_text
from tgbot.templates.messages import advertisement_reminder_message
from tgbot.utils import helpers

router = Router()


@router.callback_query(F.data.startswith("check_actual"))
async def handle_check_actual(callback: CallbackQuery):
    advertisement_id = int(callback.data.split(":")[-1])
    await callback.message.edit_text(
        text="Изменилась ли цена данного объявления ?",
        reply_markup=is_price_actual_kb(advertisement_id),
    )


#
# @router.callback_query(F.data.startswith("actual"))
# async def react_to_advertisement_actual(call: CallbackQuery):
#     """Если объявление является актуальным"""
#     await call.answer()
#
#     advertisement_id = int(call.data.split(":")[-1])
#     await call.message.edit_text(
#         text="Изменилась ли цена данного объявления ?",
#         reply_markup=is_price_actual_kb(advertisement_id),
#     )
#
#
@router.callback_query(F.data.startswith("price_changed"))
async def react_to_advertisement_price_changed(call: CallbackQuery, state: FSMContext):
    """Если цена объявления поменялась."""
    await call.answer()

    advertisement_id = int(call.data.split(":")[-1])
    await state.set_state(AdvertisementRelevanceState.new_price)
    await state.update_data(advertisement_id=advertisement_id)
    await call.message.edit_text(
        "Напишите новую цену для объявления", reply_markup=None
    )


#
#
@router.message(AdvertisementRelevanceState.new_price)
async def set_actual_price_for_advertisement(
    message: Message, repo: RequestsRepo, state: FSMContext
):
    """Добавляем новую цену объявлению."""
    state_data = await state.get_data()
    chat_id = message.chat.id

    # users data
    user = await repo.users.get_user_by_chat_id(chat_id)
    director_chat_id = user.added_by

    # advertisement data
    advertisement_id = state_data.get("advertisement_id")
    advertisement = await repo.advertisements.get_advertisement_by_id(advertisement_id)
    operation_type = advertisement.operation_type.value

    new_reminder_days = helpers.get_revminder_time_for_advertisement(operation_type)

    new_price = helpers.filter_digits(message.text)

    updated_advertisement = await repo.advertisements.update_advertisement(
        advertisement_id=advertisement_id,
        price=int(new_price),
        new_price=int(new_price),
        reminder_time=new_reminder_days,
    )

    # подготавливаем медиа группу для отправки
    media_group = await helpers.collect_media_group_for_advertisement(
        updated_advertisement, repo
    )

    await message.answer("Объявление отправлено руководителю на проверку")
    agent_fullname = f"{user.first_name} {user.lastname}"

    await message.bot.send_media_group(director_chat_id, media_group)
    await message.bot.send_message(
        director_chat_id,
        f"""
Агент: <i>{agent_fullname}</i> обновил объявление
Новая цена объявления: <b>{new_price}</b>
Объявление прошло модерацию?
""",
        reply_markup=advertisement_moderation_kb(advertisement_id=advertisement_id),
    )


@router.callback_query(F.data.startswith("price_not_changed"))
async def react_to_advertisement_price_not_changed(
    call: CallbackQuery,
    repo: RequestsRepo,
):
    """Отправляем сообщения во все группу и топики если цена не поменялась."""
    await call.answer()
    await call.message.delete()

    advertisement_id = int(call.data.split(":")[-1])

    advertisement = await repo.advertisements.get_advertisement_by_id(advertisement_id)

    advertisement_photos = await helpers.get_advertisement_photos(
        advertisement_id, repo
    )

    # получаем новое время обновления
    operation_type = advertisement.operation_type.value
    # Если тип "покупка" - прибавляем 14 дней, если "аренда" - 7 дней
    if operation_type == "Покупка":
        reminder_time = datetime.now() + timedelta(days=14)
    else:  # Аренда
        reminder_time = datetime.now() + timedelta(days=7)

    formatted_reminder_time = reminder_time.strftime("%Y-%m-%d")

    channel_name, advertisement_message = (
        helpers.get_channel_name_and_message_by_operation_type(advertisement)
    )
    media_group = helpers.get_media_group(advertisement_photos, advertisement_message)

    agent = await repo.users.get_user_by_id(advertisement.user_id)

    # обновляем дату проверки актуальности
    await repo.advertisements.update_advertisement(
        reminder_time=reminder_time, advertisement_id=advertisement_id
    )

    # отправляем сообщение в супер группу по топикам
    await helpers.send_message_to_rent_topic(
        bot=call.bot,
        price=advertisement.price,
        media_group=media_group,
        operation_type=operation_type,
    )

    if operation_type == "Покупка":
        await call.bot.send_media_group(
            chat_id=config.tg_bot.base_channel_name,
            media=media_group,
        )

    # отправляем сообщение в каналы по типу операции
    try:
        await call.bot.send_media_group(
            chat_id=channel_name,
            media=media_group,
        )
    except Exception as e:
        await call.bot.send_message(
            chat_id=config.tg_bot.test_main_chat_id,
            text=f"ошибка при отправке медиа группы\n{str(e)}",
        )

    await call.message.answer(
        f"Уведомление для проверки актуальности отправится агенту в \n<b>{formatted_reminder_time} 9:00</b>"
    )
    await call.bot.send_message(
        agent.added_by, text=advertisement_reminder_message(formatted_reminder_time)
    )

    current_date = datetime.now().strftime("%Y-%m-%d")
    advertisements_for_check = await helpers.get_user_not_actual_advertisements_by_date(
        date=current_date,
        repo=repo,
    )
    for chat_id, advertisements in advertisements_for_check.items():
        if not advertisements:
            await call.bot.send_message(
                chat_id, "Нету объявлений для проверки актуальности"
            )

        await call.bot.send_message(
            chat_id,
            f"Проверка актуальности объявлений.\nКоличество объявлений: {len(advertisements)}",
            reply_markup=actual_checking_kb(advertisements),
        )
