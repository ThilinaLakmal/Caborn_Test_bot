import re

with open('handlers/callback_handler.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('_safe_answer_callback_query(bot, ', 'bot.answer_callback_query(')
content = content.replace('_safe_edit_message_text(bot, ', 'bot.edit_message_text(')

with open('handlers/callback_handler.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Reverted successfully')
