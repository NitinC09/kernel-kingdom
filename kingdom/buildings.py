"""Building types of the `bld` grid (shared by daemon and renderer)."""

(NONE, HOUSE, STREET, PLAZA, MARKET, TEMPLE, BARRACKS, WORKSHOP, BARN, FARM, PASTURE, LUMBER,
 DOCK, WELL, GARDEN, LIBRARY, TAVERN, GRAVEYARD, WATCHTOWER, MONUMENT, PALACE) = range(21)

NAMES = {
    NONE: "", HOUSE: "house", STREET: "street", PLAZA: "plaza", MARKET: "market", TEMPLE: "temple",
    BARRACKS: "barracks", WORKSHOP: "workshop", BARN: "barn", FARM: "farm", PASTURE: "pasture",
    LUMBER: "sawmill", DOCK: "dock", WELL: "well", GARDEN: "garden", LIBRARY: "library",
    TAVERN: "tavern", GRAVEYARD: "graveyard", WATCHTOWER: "watchtower", MONUMENT: "monument",
    PALACE: "palace",
}

# Unique buildings of a city and from how many lands they appear
SPECIALS = [(MARKET, 18), (TAVERN, 28), (TEMPLE, 40), (BARRACKS, 60), (LIBRARY, 110)]
WORKPLACES = {WORKSHOP, MARKET, LUMBER, DOCK, BARN, TAVERN, LIBRARY}
