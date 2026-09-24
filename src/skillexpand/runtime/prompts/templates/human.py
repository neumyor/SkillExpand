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

FORMAT_RULES_OPERATION_TEMPLATE = """<OPERATION> <RULE NUMBER>: <RULE>

The available operations are: AGREE (if the existing rule is strongly relevant for the task), REMOVE (if one existing rule is contradictory or similar/duplicated to other existing rules), EDIT (if any existing rule is not general enough or can be enhanced, rewrite and improve it), ADD (add new rules that are very different from existing rules and relevant for other tasks). Each needs to CLOSELY follow their corresponding formatting below (any existing rule not edited, not agreed, nor removed is considered copied):

AGREE <EXISTING RULE NUMBER>: <EXISTING RULE>
REMOVE <EXISTING RULE NUMBER>: <EXISTING RULE>
EDIT <EXISTING RULE NUMBER>: <NEW MODIFIED RULE>
ADD <NEW RULE NUMBER>: <NEW RULE>

Do not mention the trials in the rules because all the rules should be GENERALLY APPLICABLE. Each rule should be concise and easy to follow. Any operation can be used MULTIPLE times. Do at most 4 operations and each existing rule can only get a maximum of 1 operation. """

CRITIQUE_SUMMARY_SUFFIX = dict(full = """Focus on REMOVE rules first, and stop ADD rule unless the new rule is VERY insightful and different from EXISTING RULES. Below are the operations you do to the above list of EXISTING RULES:
""", not_full = """Below are the operations you do to the above list of EXISTING RULES:
""")

human_critique_existing_rules_all_success_template = """{instruction}
Here are the trials:
{success_history}

Here are the EXISTING RULES:
{existing_rules}

By examining the successful trials, and the list of existing rules, you can perform the following operations: add, edit, remove, or agree so that the new list of rules are general and high level insights of the successful trials or proposed way of Thought so they can be used as helpful tips to different tasks in the future. Have an emphasis on tips that help the agent perform better Thought and Action. Follow the below format:

""" + FORMAT_RULES_OPERATION_TEMPLATE

human_all_success_existing_rules_critique = HumanMessagePromptTemplate.from_template(human_critique_existing_rules_all_success_template)

human_critique_existing_rules_template = """{instruction}
Here are the two previous trials to compare and critique:
TRIAL TASK:
{task}

SUCCESSFUL TRIAL:
{success_history}

FAILED TRIAL:
{fail_history}

Here are the EXISTING RULES:
{existing_rules}

By examining and contrasting to the successful trial, and the list of existing rules, you can perform the following operations: add, edit, remove, or agree so that the new list of rules is GENERAL and HIGH LEVEL critiques of the failed trial or proposed way of Thought so they can be used to avoid similar failures when encountered with different questions in the future. Have an emphasis on critiquing how to perform better Thought and Action. Follow the below format:

""" + FORMAT_RULES_OPERATION_TEMPLATE

human_existing_rules_critique = HumanMessagePromptTemplate.from_template(human_critique_existing_rules_template)

HUMAN_CRITIQUES = dict(
    compare_existing_rules=human_existing_rules_critique,
    all_success_existing_rules=human_all_success_existing_rules_critique,
)

RULE_TEMPLATE = dict(
    searchqa=HumanMessagePromptTemplate.from_template("""The following are some experience you gather on a similar task of question answering using Wikipedia API. Use these as references to help you perform this task:
{rules}
"""),
    alfworld=HumanMessagePromptTemplate.from_template("""The following are some experience you gather on a similar task of completing a household task by interacting in a household environment. Use these as references to help you perform this task:
{rules}
""")
)
