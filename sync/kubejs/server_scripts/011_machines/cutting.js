ServerEvents.recipes(event => {
  console.info("=== 011_machines/cutting Recipe script loaded ===");

  // ---------------------------------------------------------
  // Melon → 9 slices + 2 seeds guaranteed + 1 extra seed (25% chance)
  // ---------------------------------------------------------

  event.recipes.create.cutting(
    [
      Item.of('minecraft:melon_slice', 9),
      Item.of('minecraft:melon_seeds', 2),
      Item.of('minecraft:melon_seeds').withChance(0.25)
    ],
    'minecraft:melon'
  ).processingTime(50)
    .id('kubejs:melon_slices');


  // ---------------------------------------------------------
  // Log → stripped log + Immersive Weathering bark
  //
  // IMPORTANT:
  // We intentionally do NOT use IW bark/wood tags.
  //
  // IW does not necessarily tag every valid bark/wood relationship.
  // The registry is the source of truth:
  //
  // minecraft:birch_log
  //     ↓
  // minecraft:stripped_birch_log
  // immersive_weathering:birch_bark
  //
  // 1 log → 1 stripped log + 1 bark
  // ---------------------------------------------------------

  const itemIds = new Set();
  const barkByWoodName = new Map();
  const logItems = [];

  Item.getList().forEach(item => {
    const id = String(item.id);

    itemIds.add(id);

    // -------------------------------------------------------
    // Gather Immersive Weathering bark items
    //
    // immersive_weathering:birch_bark → birch
    // immersive_weathering:oak_bark   → oak
    // -------------------------------------------------------

    if (
      id.startsWith('immersive_weathering:') &&
      id.endsWith('_bark')
    ) {
      const itemName = id.split(':')[1];
      const woodName = itemName.substring(
        0,
        itemName.length - '_bark'.length
      );

      barkByWoodName.set(woodName, id);
    }

    // -------------------------------------------------------
    // Gather every registered *_log
    // -------------------------------------------------------

    if (id.endsWith('_log')) {
      logItems.push(id);
    }
  });

  console.info(
    `[Create Cutting] Found ${barkByWoodName.size} Immersive Weathering bark types`
  );

  console.info(
    `[Create Cutting] Found ${logItems.length} registered log items`
  );


  let added = 0;
  let skippedNoBark = 0;
  let skippedNoStrippedLog = 0;

  logItems.forEach(logId => {
    const separator = logId.indexOf(':');
    const namespace = logId.substring(0, separator);
    const itemName = logId.substring(separator + 1);

    // minecraft:birch_log → birch
    const woodName = itemName.substring(
      0,
      itemName.length - '_log'.length
    );

    // -------------------------------------------------------
    // Find matching IW bark
    //
    // birch → immersive_weathering:birch_bark
    // -------------------------------------------------------

    const barkId = barkByWoodName.get(woodName);

    if (!barkId) {
      skippedNoBark++;
      return;
    }

    // -------------------------------------------------------
    // Find stripped counterpart
    //
    // minecraft:birch_log
    //     →
    // minecraft:stripped_birch_log
    // -------------------------------------------------------

    const strippedId = `${namespace}:stripped_${itemName}`;

    if (!itemIds.has(strippedId)) {
      skippedNoStrippedLog++;
      return;
    }

    console.info(
      `[Create Cutting] ${logId} → ${strippedId} + ${barkId}`
    );

    // -------------------------------------------------------
    // Remove the existing Create stripping recipe
    // -------------------------------------------------------

    event.remove({
      type: 'create:cutting',
      input: logId,
      output: strippedId
    });

    // -------------------------------------------------------
    // Add replacement recipe
    //
    // 1 log → 1 stripped log + 1 bark
    // -------------------------------------------------------

    event.recipes.create.cutting(
      [
        Item.of(strippedId),
        Item.of(barkId)
      ],
      logId
    )
      .processingTime(50)
      .id(`kubejs:create_cutting/${namespace}/${itemName}_bark`);

    added++;
  });


  // ---------------------------------------------------------
  // Diagnostics
  // ---------------------------------------------------------

  console.info(
    `[Create Cutting] Added ${added} log → stripped log + bark recipes`
  );

  console.info(
    `[Create Cutting] Skipped ${skippedNoBark} logs with no matching IW bark`
  );

  console.info(
    `[Create Cutting] Skipped ${skippedNoStrippedLog} logs with no stripped-log counterpart`
  );
});
