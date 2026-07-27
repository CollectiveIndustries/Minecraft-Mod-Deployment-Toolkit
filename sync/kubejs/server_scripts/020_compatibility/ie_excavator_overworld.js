// Immersive Engineering Excavator Mineral Deposits (Overworld)
ServerEvents.recipes(event => {

  // Lithium Pegmatite
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

  // Banded Iron Actinide
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

  // Metamorphic Luminite Intrusion
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

});