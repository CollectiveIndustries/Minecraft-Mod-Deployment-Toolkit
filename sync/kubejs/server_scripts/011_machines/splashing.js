ServerEvents.recipes(event => {

  // Sand → Gold Nugget (15%) + Clay Ball (25%)
  event.recipes.create.splashing(
    [
      Item.of('minecraft:gold_nugget').withChance(0.15),
      Item.of('minecraft:clay_ball').withChance(0.25)
    ],
    'minecraft:sand',
    125
  ).id('kubejs:create_sand_plant');

  // Cinder Flour → Redstone (10%)
  event.recipes.create.splashing(
    Item.of('minecraft:redstone').withChance(0.10),
    'create:cinder_flour',
    10
  ).id('kubejs:create_redstone_plant');

});