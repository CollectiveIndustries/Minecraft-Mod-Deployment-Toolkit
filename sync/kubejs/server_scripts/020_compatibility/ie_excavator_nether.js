// Immersive Engineering Excavator Mineral Deposit (Nether Sulfur)
ServerEvents.recipes(event => {
  console.info("=== 020_compatibility/ie_excavator_nether Recipe script loaded ===");

  event.custom({
    type: 'immersiveengineering:mineral_mix',
    dimensions: ['minecraft:the_nether'],
    weight: 28,
    fail_chance: 0.07,
    ores: [
      { chance: 0.60, output: { item: 'create_more_features:sulfur_ore' } },
      { chance: 0.30, output: { item: 'tfmg:sulfur' } },
      { chance: 0.20, output: { item: 'biomesoplenty:brimstone' } }
    ],
    spoils: [
      { chance: 0.20, output: { item: 'minecraft:basalt' } },
      { chance: 0.20, output: { item: 'minecraft:blackstone' } },
      { chance: 0.15, output: { item: 'minecraft:magma_block' } },
      { chance: 0.15, output: { item: 'minecraft:obsidian' } },
      { chance: 0.10, output: { item: 'minecraft:crying_obsidian' } }
    ]
  }).id('kubejs:hydrothermal_sulfur_vent');

});