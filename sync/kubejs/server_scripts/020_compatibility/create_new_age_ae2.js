ServerEvents.recipes(event => {

  // Certus Quartz → Charged Certus Quartz (4,000 FE)
  event.custom({
    type: 'create_new_age:energising',
    energy_needed: 4000,
    ingredients: [
      { item: 'ae2:certus_quartz_crystal' }
    ],
    results: [
      { item: 'ae2:charged_certus_quartz_crystal' }
    ]
  }).id('kubejs:charge_certus_quartz');

  // Amethyst Shard → Fluix Crystal (8,000 FE)
  event.custom({
    type: 'create_new_age:energising',
    energy_needed: 8000,
    ingredients: [
      { item: 'minecraft:amethyst_shard' }
    ],
    results: [
      { item: 'ae2:fluix_crystal' }
    ]
  }).id('kubejs:amethyst_to_fluix');

});