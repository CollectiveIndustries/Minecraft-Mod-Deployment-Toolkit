#!/bin/sh

case "$(date +%M)" in
    00)
        docker exec mc-survival rcon-cli time set noon
        ;;
    15)
        docker exec mc-survival rcon-cli time set night
        ;;
    30)
        docker exec mc-survival rcon-cli time set midnight
        ;;
    45)
        docker exec mc-survival rcon-cli time set day
        ;;
esac
