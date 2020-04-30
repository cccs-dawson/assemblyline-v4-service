from __future__ import annotations

import logging
from typing import List, Union, Optional, Dict, Any

from assemblyline.common import forge
from assemblyline.common import log as al_log
from assemblyline.common.attack_map import attack_map
from assemblyline.common.classification import InvalidClassification
from assemblyline.common.dict_utils import unflatten
from assemblyline.common.str_utils import StringTable, safe_str
from assemblyline_v4_service.common.helper import get_service_attributes, get_heuristics

al_log.init_logging('service.result')
log = logging.getLogger('assemblyline.service.result')

Classification = forge.get_classification()
SERVICE_ATTRIBUTES = get_service_attributes()

BODY_FORMAT = StringTable('BODY_FORMAT', [
    ('TEXT', 0),
    ('MEMORY_DUMP', 1),
    ('GRAPH_DATA', 2),
    ('URL', 3),
    ('JSON', 4),
    ('KEY_VALUE', 5),
])


class InvalidHeuristicException(Exception):
    pass


class ResultAggregationException(Exception):
    pass


HEUR_LIST = get_heuristics()


def get_heuristic_primitives(heur: Heuristic):
    if heur is None:
        return None

    return dict(
        heur_id=heur.heur_id,
        score=heur.score,
        attack_ids=heur.attack_ids,
        signatures=heur.signatures,
        frequency=heur.frequency
    )


class Heuristic:
    def __init__(self, heur_id: int,
                 attack_id: Optional[str] = None,
                 signature: Optional[str] = None,
                 attack_ids: Optional[List[str]] = None,
                 signatures: Optional[Dict[(str, None), int]] = None,
                 frequency: Optional[int] = 1):
        if heur_id not in HEUR_LIST:
            raise InvalidHeuristicException(f"Invalid heuristic. A heuristic with ID: {heur_id}, must be added to "
                                            f"the service manifest before using it.")
        self.definition = HEUR_LIST[heur_id]
        self.heur_id = heur_id
        self.attack_ids = []
        self.score = 0
        self.frequency = 0

        # Default attack_id list is either empty or received attack_ids parameter
        attack_ids = attack_ids or []

        # If an attack_id is specified, append it to attack id list
        if attack_id:
            attack_ids.append(attack_id)

        # If no attack_id are set, check heuristic definition for a default attack id
        if not attack_ids and self.definition.attack_id:
            attack_ids.extend(self.definition.attack_id)

        # Validate that all attack_ids are in the attack_map
        for a_id in attack_ids:
            if a_id in attack_map:
                self.attack_ids.append(a_id)
            else:
                log.warning(f"Invalid attack_id '{a_id}' for heuristic '{heur_id}'. Ignoring it.")

        # Signature map is either the provided value or an empty map
        self.signatures = signatures or {}

        # If a signature is provided, add it to the map and increment its frequency
        if signature:
            self.signatures.setdefault(signature, 0)
            self.signatures[signature] += 1

        # If there are no signatures, add an empty signature with frequency of one (signatures drives the score)
        if not self.signatures:
            self.frequency = frequency
            self.score += self.definition.score * frequency

        # For each signatures, check if they are in the score_map and compute the score based of their frequency
        for sig_name, freq in self.signatures.items():
            if sig_name in self.definition.signature_score_map:
                self.score += self.definition.signature_score_map[sig_name] * freq
            else:
                self.score += self.definition.score * freq

    def add_attack_id(self, attack_id: str):
        # Check if this is a valid attack ID
        if attack_id not in attack_map:
            log.warning(f"Invalid attack_id '{attack_id}' for heuristic '{self.heur_id}'. Ignoring it.")
            return

        # if its a new attack id, add it to the list
        if attack_id not in self.attack_ids:
            self.attack_ids.append(attack_id)

    def add_signature_id(self, signature: str, frequency: int = 1):
        # Add the signature to the map and adds it new frequency to the old value
        self.signatures.setdefault(signature, 0)
        self.signatures[signature] += frequency

        # Compute the new score based of the signature that was just added
        if signature in self.definition.signature_score_map:
            self.score += self.definition.signature_score_map[signature] * frequency
        else:
            self.score += self.definition.score * frequency

    def increment_frequency(self, frequency: int = 1):
        # Increment the signature less frequency of the heuristic
        self.frequency += frequency

        # Compute the new score based of that new frequency
        self.score += self.definition.score * frequency


class ResultSection:
    def __init__(
            self,
            title_text: Union[str, List],
            body: Optional[str, Dict] = None,
            classification: Optional[Classification] = None,
            body_format: BODY_FORMAT = BODY_FORMAT.TEXT,
            heuristic: Optional[Heuristic] = None,
            tags: Optional[Dict[str, List[str]]] = None,
            parent: Optional[Union[ResultSection, Result]] = None,
    ):
        self._finalized: bool = False
        self.parent = parent
        self._section = None
        self.subsections: List[ResultSection] = []
        self.body: str = body
        self.classification: Classification = classification or SERVICE_ATTRIBUTES.default_result_classification
        self.body_format: BODY_FORMAT = body_format
        self.depth: int = 0
        self.tags = tags or {}
        self.heuristic = None

        if isinstance(title_text, list):
            title_text = ''.join(title_text)
        self.title_text = safe_str(title_text)

        if heuristic:
            if not isinstance(heuristic, Heuristic):
                log.warning(f"This is not a valid Heuristic object: {str(heuristic)}")
            else:
                self.heuristic = heuristic

        if parent is not None:
            if isinstance(parent, ResultSection):
                parent.add_subsection(self)
            elif isinstance(parent, Result):
                parent.add_section(self)

    def add_line(self, text: Union[str, List]) -> None:
        # add_line with a list should join without newline seperator.
        # use add_lines if list should be split one element per line.
        if isinstance(text, list):
            text = ''.join(text)
        textstr = safe_str(text)
        if self.body:
            textstr = '\n' + textstr
            self.body = self.body + textstr
        else:
            self.body = textstr

    def add_lines(self, line_list: List[str]) -> None:
        if not isinstance(line_list, list):
            log.warning(f"add_lines called with invalid type: {type(line_list)}. ignoring")
            return

        segment = '\n'.join(line_list)
        if self.body is None:
            self.body = segment
        else:
            self.body = self.body + '\n' + segment

    def add_subsection(self, subsection: ResultSection, on_top: bool = False) -> None:
        """
        Add a result subsection to another result section or subsection.

        :param subsection: Subsection to add to another result section or subsection
        :param on_top: Display this result section on top of other subsections
        """
        if on_top:
            self.subsections.insert(0, subsection)
        else:
            self.subsections.append(subsection)
        subsection.parent = self

    def add_tag(self, tag_type: str, value: Union[str, bytes]) -> None:
        if isinstance(value, bytes):
            value = value.decode()

        if tag_type not in self.tags:
            self.tags[tag_type] = []

        if value not in self.tags[tag_type]:
            self.tags[tag_type].append(value)

    def finalize(self, depth: int = 0) -> bool:
        if self._finalized:
            raise ResultAggregationException("Double finalize() on result detected.")
        self._finalized = True

        keep_me = True
        tmp_subs = []
        self.depth = depth
        for subsection in self.subsections:
            subsection.finalize(depth=depth+1)
            # Unwrap it if we're going to keep it
            if subsection in self.subsections:
                tmp_subs.append(subsection)
        self.subsections = tmp_subs

        # At this point, all subsections are finalized and we're not deleting ourself
        if self.parent is not None and isinstance(self.parent, ResultSection):
            try:
                self.parent.classification = \
                    Classification.max_classification(self.classification, self.parent.classification)
            except InvalidClassification as e:
                log.error(f"Failed to finalize section due to a classification error: {str(e)}")
                keep_me = False

        return keep_me

    def set_body(self, body: str, body_format: BODY_FORMAT = BODY_FORMAT.TEXT) -> None:
        self.body = body
        self.body_format = body_format

    def set_heuristic(self, heur_id: int, attack_id: Optional[str] = None, signature: Optional[str] = None) -> None:
        """
        Set a heuristic for a result section/subsection.
        A heuristic is required to assign a score to a result section/subsection.

        :param heur_id: Heuristic ID as set in the service manifest
        :param attack_id: (optional) Attack ID related to the heuristic
        :param signature: (optional) Signature Name that triggered the heuristic
        """

        if self.heuristic:
            raise InvalidHeuristicException(f"The service is trying to set the heuristic twice, this is not allowed. "
                                            f"[Current: {self.heuristic.heur_id}, New: {heur_id}]")

        self.heuristic = Heuristic(heur_id, attack_id=attack_id, signature=signature)


class Result:
    def __init__(self, sections: Optional[List[ResultSection]] = None) -> None:
        self._flattened_sections: List[Dict[str, Any]] = []
        self._score: int = 0
        self.sections: List[ResultSection] = sections or []

    def _append_section(self, section: ResultSection) -> None:
        self._flattened_sections.append(dict(
            body=section.body,
            classification=section.classification,
            body_format=section.body_format,
            depth=section.depth,
            heuristic=get_heuristic_primitives(section.heuristic),
            tags=unflatten(section.tags),
            title_text=section.title_text,
        ))

    def _flatten_sections(self, section: ResultSection, root: bool = True) -> None:
        if len(section.subsections) > 0:
            if root:
                self._append_section(section)

            for subsection in section.subsections:
                self._append_section(subsection)
                if len(subsection.subsections) > 0:
                    self._flatten_sections(subsection, root=False)
        else:
            self._append_section(section)

    def add_section(self, section: ResultSection, on_top: bool = False) -> None:
        """
        Add a result section to the root of the result.

        :param section: Section to add to the root of the result
        :param on_top: Display this result section on top of other sections
        """
        if on_top:
            self.sections.insert(0, section)
        else:
            self.sections.append(section)

    def finalize(self) -> Dict[str, Any]:
        to_delete_sections = []

        for section in self.sections:
            section.parent = self
            if not section.finalize():
                to_delete_sections.append(section)

        # Delete sections we can't keep
        for section in to_delete_sections:
            self.sections.remove(section)

        # Flatten all the sections into a flat list
        for section in self.sections:
            self._flatten_sections(section)

        for section in self._flattened_sections:
            heuristic = section.get('heuristic')
            if heuristic:
                self._score += heuristic['score']

        result = dict(
            score=self._score,
            sections=self._flattened_sections,
        )

        return result
