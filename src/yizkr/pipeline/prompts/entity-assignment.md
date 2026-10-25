Refer to EVERY entity — subjects and objects alike — with a "local" reference whose local_id is the entity's manifest id and whose label is the manifest's canonical label. Ids outside the manifest are rejected. The full manifest (id = canonical label):

{roster_block}

Emit every claim whose SUBJECT is one of your ASSIGNED entities below — and only those. Relations to entities outside your assignment (their parents, unions, locations) still belong to you when your entity is the subject; refer to the other entity by its manifest id. Every claim whose subject is the document itself or an image it places (`issued_by`, `author`, `addressee`, `inception`, `location_of_creation`, `main_subject`, `shows`, and every `caption` claim) belongs to the chunk assigned {first_entity}, the first entity assigned for extraction, whatever its value. A claim about a document the text mentions, whose subject is that document's manifest entity, belongs to the chunk that entity is assigned to, as any claim whose subject it is. Do not emit claims whose subject is not assigned to you, and do not emit instance_of claims (the manifest's kind already records it).

Assigned entities:

{entities_block}
