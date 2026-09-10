

- selection buttons, we have different options like select all, missing etc. but need option to select 'same' as well.



## Major updates 
- The windows.py manages the different states like self.removed = False etc. internally, there should be state machines probably managing different states?




## resolved issues:

- if i connect to a windows system as destination, the available disk space shows some message that info not available. however it calculates the folder sizes properly.

- transfer panel: copy speed and ETA info: it shows the speed of reading files, not the actual contents that are copied/writen. so if read is fast and write is slow, it shows a "merging' status on porgress bar and goes in irresponsive state, need to discuss and update behavior. (usually eventually the tansfer completes after some wait. but i keep getting messages from operating system to wait or kill/quit till it finishes).

- The file available disk space do not update after transfer finish, on even on the reload/refresh button click in destination. i have to navigate to different folder to get it updated. it also not updating on deleting files using delete button.

