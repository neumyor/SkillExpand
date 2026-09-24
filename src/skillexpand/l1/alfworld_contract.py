"""ALFWorld-only execution contract shared by acting and learning prompts."""
INSTRUCTIONS = '''You act in the ALFWorld text household simulator.
Use exact object/receptacle IDs from actual observations and admissible commands.
Commands: go to <receptacle>; open/close <receptacle>; examine <object/receptacle>;
take <object> from <receptacle>; move <held object> to <receptacle>; inventory;
use <lamp>; clean <held object> with <sinkbasin>; heat <held object> with <microwave>;
cool <held object> with <fridge>. Use > action or > think: reasoning syntax.
This TextWorld setup uses move ... to ... for placement, not put ... in/on ... .
Admissible commands describe the CURRENT state; use them rather than inventing syntax.
Check possession: this setup allows one carried object. Place an irrelevant held
object before taking another; deliver two-object goals one object at a time.
Never substitute pot for pan, bowl for plate, or an imagined object for an observed ID.
An open empty container is empty: do not invent hidden contents or repeatedly examine it.
There is no camera-orientation command or abstract verify-goal action. A failed action
can reflect location, possession or prerequisites, not necessarily a syntax problem.
For lamp goals hold the requested object and use the lamp. Only environment won feedback
confirms completion; thoughts or apparent placement do not establish success.
Every new trial resets inventory, object placement, open/closed and processed state.
Prior observed locations are task-local leads to recheck, not universal facts.
Remember checked empty locations within a trial; try unvisited locations and reserve
steps for taking, processing and delivery. Consecutive identical actions terminate
this wrapper's episode, even for look/examine. Respect the stated step budget.'''
