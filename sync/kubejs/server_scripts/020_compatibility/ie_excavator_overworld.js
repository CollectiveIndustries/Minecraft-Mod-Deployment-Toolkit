// Immersive Engineering Excavator Mineral Deposits (Overworld)
ServerEvents.recipes(event => {
  console.info("=== 020_compatibility/ie_excavator_overworld Recipe script loaded ===");

  // ============================================================
  // Lithium Pegmatite
  // ============================================================
  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:overworld'],
    weight: 45,
    fail_chance: 0.05,
    ores: [
      { chance: 0.70, output: { item: 'tfmg:deepslate_lithium_ore' } },
      { chance: 0.20, output: { item: 'create_unbreakable:luminarchy_block' } },
      { chance: 0.15, output: { item: 'minecraft:amethyst_block' } }
    ],
    spoils: [
      { chance: 0.5, output: { item: 'minecraft:granite' } },
      { chance: 0.5, output: { item: 'minecraft:cobbled_deepslate' } }
    ]
  }).id('kubejs:lithium_pegmatite');

  // ============================================================
  // Banded Iron Actinide
  // ============================================================
  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:overworld'],
    weight: 30,
    fail_chance: 0.08,
    ores: [
      { chance: 0.65, output: { item: 'create_new_age:magnetite_block' } },
      { chance: 0.20, output: { item: 'create_new_age:thorium_ore' } }
    ],
    spoils: [
      { chance: 0.5, output: { item: 'minecraft:tuff' } },
      { chance: 0.5, output: { item: 'minecraft:cobblestone' } }
    ]
  }).id('kubejs:banded_iron_actinide');

  // ============================================================
  // Metamorphic Luminite Intrusion
  // ============================================================
  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:overworld'],
    weight: 18,
    fail_chance: 0.12,
    ores: [
      { chance: 0.55, output: { item: 'create_unbreakable:philolite_block' } },
      { chance: 0.30, output: { item: 'create_unbreakable:luminarchy_block' } }
    ],
    spoils: [
      { chance: 0.5, output: { item: 'minecraft:andesite' } },
      { chance: 0.5, output: { item: 'minecraft:cobbled_deepslate' } }
    ]
  }).id('kubejs:metamorphic_luminite_intrusion');

  // ============================================================
  // Carboniferous Carbonate
  //
  // Lignite + limestone + dripstone + tuff.
  //
  // Limestone is deliberately kept modest because your custom
  // limestone crusher chain produces a very broad range of
  // Create-related resources.
  // ============================================================
  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:overworld'],
    weight: 24,
    fail_chance: 0.08,
    ores: [
      { chance: 0.70, output: { item: 'tfmg:lignite' } },
      { chance: 0.30, output: { item: 'create:limestone' } },
      { chance: 0.35, output: { item: 'minecraft:dripstone_block' } },
      { chance: 0.25, output: { item: 'minecraft:tuff' } }
    ],
    spoils: [
      { chance: 0.5, output: { item: 'minecraft:stone' } },
      { chance: 0.5, output: { item: 'minecraft:gravel' } }
    ]
  }).id('kubejs:carboniferous_carbonate');

  // ============================================================
  // Hydrothermal Sulfide
  //
  // Galena + asurine with quartz and a small amount of ochrum.
  // Represents a metal-bearing hydrothermal deposit rather than
  // a single-resource ore vein.
  // ============================================================
  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:overworld'],
    weight: 20,
    fail_chance: 0.10,
    ores: [
      { chance: 0.65, output: { item: 'tfmg:galena' } },
      { chance: 0.35, output: { item: 'create:asurine' } },
      { chance: 0.20, output: { item: 'minecraft:diorite' } },
      { chance: 0.12, output: { item: 'create:ochrum' } }
    ],
    spoils: [
      { chance: 0.5, output: { item: 'minecraft:tuff' } },
      { chance: 0.5, output: { item: 'minecraft:cobblestone' } }
    ]
  }).id('kubejs:hydrothermal_sulfide');

  // ============================================================
  // Volcanic Mafic
  //
  // Veridium + scoria + tuff with a minor asurine component.
  // Copper-bearing volcanic material is the primary resource.
  // ============================================================
  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:overworld'],
    weight: 18,
    fail_chance: 0.10,
    ores: [
      { chance: 0.70, output: { item: 'create:veridium' } },
      { chance: 0.25, output: { item: 'create:scoria' } },
      { chance: 0.35, output: { item: 'minecraft:tuff' } },
      { chance: 0.15, output: { item: 'create:asurine' } }
    ],
    spoils: [
      { chance: 0.5, output: { item: 'minecraft:basalt' } },
      { chance: 0.5, output: { item: 'minecraft:cobblestone' } }
    ]
  }).id('kubejs:volcanic_mafic');

  // ============================================================
  // Felsic Metamorphic
  //
  // Crimsite with quartz-bearing diorite and a small amount of
  // ochrum. Tuff provides a secondary geological component.
  // ============================================================
  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:overworld'],
    weight: 15,
    fail_chance: 0.08,
    ores: [
      { chance: 0.70, output: { item: 'create:crimsite' } },
      { chance: 0.25, output: { item: 'minecraft:diorite' } },
      { chance: 0.15, output: { item: 'create:ochrum' } },
      { chance: 0.20, output: { item: 'minecraft:tuff' } }
    ],
    spoils: [
      { chance: 0.5, output: { item: 'minecraft:stone' } },
      { chance: 0.5, output: { item: 'minecraft:andesite' } }
    ]
  }).id('kubejs:felsic_metamorphic');

});
