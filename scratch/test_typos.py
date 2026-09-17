import re

t = "Thammasat University —“‘“‘“CS;S*™SC‘idzd"
print(repr(re.sub(r"\s*[—–].*$", "", t)))

t2 = "Universidad de las Américas Puebla (UDLAP’"
t2 = re.sub(r"\(([A-Za-z0-9&.+ -]+)[’\']", r"(\1)", t2)
print(repr(t2))
