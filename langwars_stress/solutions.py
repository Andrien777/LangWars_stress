"""Player programs used by the workload.

The reference implementations mirror the public default tasks. They are copied
here so the stress pack is standalone and never imports server code.
"""
from __future__ import annotations

TASK_SOLUTIONS = {
    "shared-sum": "@solution fn <int,int> sum_answer(left,right){return left+right;}",
    "cyclic-access": "@solution fn <list,int> cyclic_answer(items,index){return get(items,index);}",
    "list-sum": """@solution fn <list> list_answer(items){
        let total=0; let index=0;
        while(index<length(items)){total=total+get(items,index);index=index+1;}
        return total;
    }""",
    "mirror-text": """@solution fn <string> mirror_answer(text){
        let result="";let index=length(text)-1;
        while(index>=0){result=concat(result,get(text,index));index=index-1;}
        return result;
    }""",
    "positive-count": """@solution fn <list> positive_count_answer(items){
        let count=0;let index=0;
        while(index<length(items)){if(get(items,index)>0){count=count+1;}index=index+1;}
        return count;
    }""",
    "range-total": """@solution fn <int,int> range_total_answer(low,high){
        let total=0;let current=low;
        while(current<=high){total=total+current;current=current+1;}
        return total;
    }""",
    "text-window": """@solution fn <string,int,int> text_window_answer(text,start,count){
        let result="";let index=0;
        while(index<count){result=concat(result,get(text,start+index));index=index+1;}
        return result;
    }""",
    "unique-values": """@solution fn <list> unique_values_answer(items){
        let result=[];let index=0;
        while(index<length(items)){
            let item=get(items,index);
            if(not contains(result,item)){push_back(result,item);}
            index=index+1;
        }
        return result;
    }""",
    "dictionary-tally": """@solution fn <list> dictionary_tally_answer(items){
        let result={};let index=0;
        while(index<length(items)){
            let item=get(items,index);set(result,item,get(result,item)+1);index=index+1;
        }
        return result;
    }""",
    "running-median": """@solution fn <list> running_median_answer(items){
        let ordered=[];let medians=[];let cursor=0;
        while(cursor<length(items)){
            push_back(ordered,get(items,cursor));
            let position=length(ordered)-1;
            while(position>0){
                if(get(ordered,position)<get(ordered,position-1)){
                    let temporary=get(ordered,position);
                    set(ordered,position,get(ordered,position-1));
                    set(ordered,position-1,temporary);
                }
                position=position-1;
            }
            push_back(medians,get(ordered,(length(ordered)-1)/2));cursor=cursor+1;
        }
        return medians;
    }""",
}

# Cheap tasks are the default because they keep worker execution time low and
# still exercise publication, queues and rechecks under concurrent players.
DEFAULT_TASKS = ("shared-sum", "cyclic-access", "list-sum", "mirror-text", "positive-count")


def task_key(task: dict) -> str:
    return str(task.get("id") or task.get("slug") or "")


def correct_source(task: dict) -> str:
    key = task_key(task)
    source = TASK_SOLUTIONS.get(key)
    if source is None:
        source = TASK_SOLUTIONS.get(str(task.get("slug") or ""))
    if source is None:
        starter = task.get("starterCode")
        if isinstance(starter, str) and starter.strip():
            return starter
        entry = str(task.get("entryPoint") or "solve")
        return f"@solution fn {entry}(){{return 0;}}"
    return source


def wrong_source(task: dict) -> str:
    starter = task.get("starterCode")
    if isinstance(starter, str) and starter.strip():
        return starter
    entry = str(task.get("entryPoint") or "solve")
    return f"@solution fn {entry}(){{return 0;}}"


def syntax_error_source(task: dict) -> str:
    return "@solution fn broken( {"


def timeout_source(task: dict) -> str:
    source = correct_source(task)
    brace = source.find("{")
    if brace < 0:
        return source
    return source[:brace + 1] + "while(1<2){}" + source[brace + 1:]


def oversized_source() -> str:
    base = "@solution fn solve(){return 0;}"
    return base + " " * (65537 - len(base))


def pathological_nesting(depth: int = 90) -> bytes:
    return (b'{"code":' + b"[" * depth + b"0" + b"]" * depth + b"}")
