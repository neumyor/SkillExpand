from langchain.prompts.chat import HumanMessagePromptTemplate, SystemMessagePromptTemplate

human_instruction_fewshots_template = """{instruction}

{fewshots}

(END OF EXAMPLES)
"""
human_instruction_fewshot_message_prompt = lambda message_type:\
    SystemMessagePromptTemplate.from_template(
        human_instruction_fewshots_template,
    ) if message_type == 'all_system' else\
        HumanMessagePromptTemplate.from_template(human_instruction_fewshots_template,)

human_task_template = """Now it's your turn!
{task}"""
human_task_message_prompt = HumanMessagePromptTemplate.from_template(
    human_task_template,
)


RULE_TEMPLATE = dict(
    searchqa=HumanMessagePromptTemplate.from_template("""The following are some experience you gather on a similar task of question answering using Wikipedia API. Use these as references to help you perform this task:
{rules}
"""),
    alfworld=HumanMessagePromptTemplate.from_template("""The following are some experience you gather on a similar task of completing a household task by interacting in a household environment. Use these as references to help you perform this task:
{rules}
""")
)
